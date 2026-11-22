"""Recipes (waggle/recipes.py) on fake ActivityWatch data: a real pattern is found, noise is ignored, privacy holds, and
nothing runs before the owner's yes. No real ActivityWatch, Ollama, Telegram, tools or PC."""
import datetime as dt
import json

import pytest
from engine_fakes import FakeBotApi, FakeClaude, FakeClock

from waggle import chat_engine, notify, outreach_ev, recipes, routines
from waggle import aw_sensor
from waggle.chat_session import ChatSession
from waggle.config import Settings, data_path
from waggle.core import outbox, store
from waggle.delivery import BotApiDelivery

CLASSROOM = ("chrome", "classroom.google.com", "school", 180)
DOCS = ("chrome", "docs.google.com", "school", 240)
CALC = ("calculatorapp", "", "", 120)
TOOLS = {"google_classroom_due", "google_calendar_list", "google_tasks_list", "remind_me"}
OWNER = "telegram:42"


def at(day, h, m=0):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def is_school_day(day):
    return dt.date(2026, 11, day).weekday() < 5


SCHOOL_DAYS = [d for d in range(2, 21) if is_school_day(d)]   # Mon 2 Nov .. Fri 20 Nov
NOW = at(20, 18)


def chain_rows(start, steps):
    """Back-to-back rows (30 s apart) in the shape aw_sensor stores."""
    out, t = [], start
    for app, host, project, secs in steps:
        out.append({"start": t, "secs": float(secs), "app": app, "project": project, "ws": "", "host": host,
                    "tid": "t", "afk": 0})
        t += secs + 30
    return out


def school_rows(days=SCHOOL_DAYS, minute=0, noise=True):
    rows = []
    for i, d in enumerate(days):
        rows += chain_rows(at(d, 9, 30), [("code", "", "coding", 1500)])
        rows += chain_rows(at(d, 16, minute + (i % 3) * 5), [CLASSROOM, *([("explorer", "", "", 60)] if noise else []),
                                                              DOCS, ("code", "", "coding", 20) if noise and i % 4 == 0
                                                              else DOCS, CALC])
    return rows


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.engine_chats = ["42"]
    settings.save()
    return Settings.load()


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.close_all()


class FakeChat:
    """A stand-in for ollama.chat_json: records what it was sent and answers with `reply`."""

    def __init__(self, reply=None):
        self.reply, self.calls = reply, []

    def __call__(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.reply is None:
            raise RuntimeError("model down")
        return self.reply


GOOD = {"name": "School check", "summary": "I will check what is due in Classroom and read your tasks.",
        "saves": "It spares you the lookup in Classroom.",
        "steps": [{"tool": "google_classroom_due", "days": 3}, {"tool": "google_tasks_list"}]}


def ready_recipe(s, chat=None, rows=None, tools=TOOLS):
    """Mine, draft and shadow the school pattern; returns the recipe."""
    rows = school_rows() if rows is None else rows
    out = recipes.refresh(s, NOW, rows, tools, chat or FakeChat(GOOD))
    assert out["drafted"] >= 1, out
    return next(r for r in recipes.all_recipes() if r["status"] in ("shadow", "drafted"))


# ---- mining -----------------------------------------------------------------------------------------------------

def test_a_real_weekday_pattern_is_found_with_noise_between_the_steps():
    found = recipes.mine(school_rows(), NOW)
    top = found[0]
    assert top["pattern"] == ["classroom.google.com", "docs.google.com", "calculatorapp"]
    assert top["evidence"]["count"] == len(SCHOOL_DAYS) and top["evidence"]["days"] == len(SCHOOL_DAYS)
    assert top["evidence"]["facets"] == {"hosts": ["classroom.google.com", "docs.google.com"], "projects": ["school"]}
    # the two-step sub-patterns have the same support, so only the longest stays
    assert [p for p in found if len(p["pattern"]) < 3 and set(p["pattern"]) <= set(top["pattern"])] == []


def test_noise_is_ignored():
    rows = []
    # different programs every day: nothing repeats
    pool = [f"app{n}" for n in range(15)]
    for i, d in enumerate(SCHOOL_DAYS[:5]):
        rows += chain_rows(at(d, 10 + i), [(a, "", "", 120) for a in pool[3 * i:3 * i + 3]])
    # three times on ONE day: not enough days
    for k in range(3):
        rows += chain_rows(at(2, 20 + k // 3, k * 15), [("vls", "", "laser", 120), ("blender", "", "cad", 120)])
    # twice on two days: not enough occurrences
    for d in (3, 4):
        rows += chain_rows(at(d, 18), [("freecad", "", "cad", 120), ("orcaslicer", "", "cad", 120)])
    # glances, system windows, AFK rows and leisure break nothing and add nothing
    for d in SCHOOL_DAYS[:6]:
        rows += chain_rows(at(d, 13), [("code", "", "coding", 10), ("explorer", "", "", 300), ("steam", "", "games", 600),
                                       ("chrome", "youtube.com", "video", 600)])
        rows.append({"start": at(d, 14), "secs": 900.0, "app": "", "project": "afk", "ws": "", "host": "", "tid": "",
                     "afk": 1})
    assert recipes.mine(rows, NOW) == []


def test_leisure_ends_an_episode_so_it_cannot_glue_two_halves_together():
    rows = []
    for d in SCHOOL_DAYS[:6]:
        rows += chain_rows(at(d, 16), [("code", "", "coding", 120), ("steam", "", "games", 120), ("obsidian", "", "notes", 120)])
    assert recipes.mine(rows, NOW) == []


def test_a_pattern_needs_three_episodes_on_two_days_exactly():
    two_days = [d for d in SCHOOL_DAYS[:2]]
    rows = school_rows(two_days)                       # 2 episodes on 2 days
    assert recipes.mine(rows, NOW) == []
    rows = school_rows(two_days) + chain_rows(at(2, 19), [CLASSROOM, DOCS, CALC])   # 3 episodes on 2 days
    assert [p["evidence"]["count"] for p in recipes.mine(rows, NOW)] == [3]


# ---- trigger ----------------------------------------------------------------------------------------------------

def test_trigger_is_weekdays_around_the_start_time():
    top = recipes.mine(school_rows(), NOW)[0]
    trig, why = recipes.trigger_of(top["evidence"]["occ"])
    assert why == "" and trig["weekdays"] == [0, 1, 2, 3, 4]
    assert trig["schedule"] == "weekdays 15:55" and trig["text"] == "weekdays around 4:05 PM"
    assert routines.next_run(trig["schedule"], after=NOW) is not None        # a schedule the routines module reads


def test_scattered_start_times_have_no_trigger_and_are_blocked(s):
    rows = []
    for i, d in enumerate(SCHOOL_DAYS[:6]):
        rows += chain_rows(at(d, 7 + 3 * (i % 4), 0), [CLASSROOM, DOCS, CALC])
    recipes.refresh(s, NOW, rows, TOOLS, FakeChat(GOOD))
    rec = recipes.all_recipes()[0]
    assert rec["status"] == "blocked" and rec["note"] == "no regular time of day"


# ---- drafting ---------------------------------------------------------------------------------------------------

def test_the_model_drafts_from_derived_facts_and_catalog_tools_only(s):
    chat = FakeChat(GOOD)
    rec = ready_recipe(s, chat)
    assert rec["draft"]["by"] == "model" and rec["draft"]["name"] == "school-check"
    assert [st["tool"] for st in rec["draft"]["steps"]] == ["google_classroom_due", "google_tasks_list"]
    messages, kw = chat.calls[0]
    assert set(kw) == {"schema", "settings", "timeout"}                      # the plain call: no options, no num_ctx
    prompt = json.dumps(messages)
    assert "classroom.google.com" not in prompt and "Classroom" in prompt and "num_ctx" not in prompt
    assert set(json.loads(messages[1]["content"])["tools"]) == {"google_classroom_due", "google_tasks_list"}


@pytest.mark.parametrize("bad", [
    {**GOOD, "steps": [{"tool": "google_doc_new"}]},                    # a tool outside the catalog
    {**GOOD, "steps": [{"tool": "google_calendar_list"}]},              # a catalog tool that covers nothing the owner did
    {**GOOD, "saves": "It saves you 45 minutes every day."},            # a number that is not in the evidence
    {**GOOD, "steps": []},
    "not json at all",
])
def test_a_bad_model_answer_falls_back_to_the_rule_draft(s, bad):
    rec = ready_recipe(s, FakeChat(bad))
    assert rec["draft"]["by"] == "rules"
    assert {st["tool"] for st in rec["draft"]["steps"]} == {"google_classroom_due", "google_tasks_list"}


def test_with_the_model_down_the_rules_draft_still_works(s):
    assert ready_recipe(s, FakeChat(None))["draft"]["by"] == "rules"


def test_a_step_whose_tool_is_not_registered_is_never_offered(s):
    rows = school_rows()
    recipes.refresh(s, NOW, rows, {"remind_me"}, FakeChat(GOOD))
    rec = recipes.all_recipes()[0]
    assert rec["status"] == "blocked" and "no tool yet" in rec["note"]
    out = recipes.offer(s, NOW)
    assert "offered" not in out


def test_work_no_tool_covers_stays_blocked_and_never_calls_the_model(s):
    rows = []
    for d in SCHOOL_DAYS[:6]:
        rows += chain_rows(at(d, 17), [("chrome", "onshape.com", "onshape", 600), ("code", "", "coding", 120),
                                       ("vls", "", "laser", 300)])
    chat = FakeChat(GOOD)
    recipes.refresh(s, NOW, rows, TOOLS, chat)
    rec = recipes.all_recipes()[0]
    assert rec["pattern"] == ["onshape.com", "code", "vls"] and rec["status"] == "blocked"
    assert chat.calls == [] and recipes.offer(s, NOW)["skipped"].startswith("nothing ready")


# ---- shadow -----------------------------------------------------------------------------------------------------

def test_shadow_logs_what_it_would_have_done_and_runs_nothing(s, monkeypatch):
    from waggle import google
    for name in ("classroom_due", "tasks_open", "calendar_list", "docs_new"):
        monkeypatch.setattr(google, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("a tool ran")))
    rec = ready_recipe(s)
    log = rec["shadow"]
    assert len(log) >= recipes.SHADOW_NEEDED and all(e["ran"] is False for e in log)
    assert all(e["would"][0].startswith("check what is due in Classroom") for e in log)
    assert all(e["owner_did_it"] for e in log) and recipes.ready(rec, NOW)
    assert routines._load() == {} and outbox.list() == []                 # no routine, no message


def test_shadow_counts_a_day_the_owner_was_there_and_did_not_do_it(s):
    rows = school_rows(SCHOOL_DAYS[:8])                                     # the habit, then it stops...
    rows += chain_rows(at(18, 16, 5), [("code", "", "coding", 1200)])       # ...on Wed 18 the owner is at the PC, other work
    rows += chain_rows(at(19, 8, 0), [("code", "", "coding", 600)])         # Thu 19: not around 16:00: no evidence
    rec = ready_recipe(s, rows=rows)
    stats = recipes.shadow_stats(rec, NOW)
    assert {e["date"]: e["owner_did_it"] for e in rec["shadow"]}["2026-11-18"] is False
    assert "2026-11-19" not in {e["date"] for e in rec["shadow"]}
    assert stats["hits"] == 8 and stats["days"] == 9


def test_a_weak_habit_is_not_ready(s):
    rows = school_rows(SCHOOL_DAYS[:4])
    for d in SCHOOL_DAYS[4:12]:
        rows += chain_rows(at(d, 16), [("code", "", "coding", 1200)])
    rec = ready_recipe(s, rows=rows)
    assert not recipes.ready(rec, NOW) and recipes.shadow_stats(rec, NOW)["rate"] < recipes.SHADOW_HIT


# ---- offering ---------------------------------------------------------------------------------------------------

@pytest.fixture
def held(s):
    s.hold_for_breaks = True
    s.save()
    return Settings.load()


def test_the_offer_is_one_held_initiative_text_in_waggles_manner(held):
    rec = ready_recipe(held)
    out = recipes.offer(held, NOW)
    assert out["offered"] == rec["code"]
    row = notify.get(out["outbox"])
    assert row["origin"] == "initiative" and row["status"] == "held"        # waits for a natural break (holdq)
    for want in ("weekdays around 4:05 PM", "Classroom, then Docs, then the calculator", f"yes {rec['code']}",
                 f"never {rec['code']}", "Nothing runs until you say yes", "check what is due in Classroom"):
        assert want in row["text"], want
    assert recipes.get(rec["code"])["status"] == "offered"
    assert routines._load() == {}


def test_at_most_one_suggestion_a_day_and_none_while_one_is_unanswered(s):
    ready_recipe(s)
    other = [("chrome", "calendar.google.com", "", 200), ("chrome", "classroom.google.com", "school", 200),
             ("code", "", "coding", 200)]
    rows = school_rows()
    for d in SCHOOL_DAYS:
        rows += chain_rows(at(d, 16, 40), other)
    recipes.refresh(s, NOW, rows, TOOLS, FakeChat(GOOD))
    assert sum(1 for r in recipes.all_recipes() if recipes.ready(r, NOW)) >= 2
    first = recipes.offer(s, NOW)
    assert "offered" in first
    assert recipes.offer(s, NOW + 3600)["skipped"].startswith("one suggestion a day")
    assert recipes.offer(s, NOW + 86400 + 3600)["skipped"] == "an earlier suggestion is still unanswered"
    assert recipes.offer(s, NOW + 5 * 86400)["offered"] != first["offered"]      # the old one lapsed unanswered


def test_quiet_hours_and_the_off_switch_hold_everything(s):
    ready_recipe(s)
    assert recipes.offer(s, at(21, 23, 30))["skipped"] == "quiet hours"
    s.recipes_enabled = False
    s.save()
    assert recipes.run(Settings.load(), NOW, school_rows(), TOOLS)["skipped"] == "recipes_enabled is off"
    assert recipes.offer(Settings.load(), NOW)["skipped"] == "recipes_enabled is off"
    assert recipes.preflight(OWNER, "yes R222", origin="user", settings=Settings.load()) is None
    assert recipes.evening_line(NOW, Settings.load()) == "" and recipes.summary(Settings.load())["enabled"] is False


def test_low_welcome_odds_hold_the_suggestion(s):
    s.outreach_ev_enabled = True
    s.save()
    ready_recipe(s)
    con = outreach_ev._db()
    for i in range(4):
        con.execute("INSERT INTO outcomes VALUES (?,?,?,?,?)", (900 + i, "recipe", "never", NOW - 86400, NOW))
    con.commit()
    con.close()
    out = recipes.offer(Settings.load(), NOW)
    assert "welcome odds" in out["skipped"] and recipes.all_recipes()[0]["status"] == "shadow"


def test_no_data_is_inert(s):
    assert recipes.run(s, NOW, rows=[], tools=TOOLS) == {"patterns": 0, "drafted": 0, "blocked": 0,
                                                         "shadow_entries": 0, "offer": {"skipped": "nothing ready (needs a shadow log of regular days)"}}
    assert recipes.all_recipes() == [] and outbox.list() == []


# ---- the answer -------------------------------------------------------------------------------------------------

def offered(s):
    rec = ready_recipe(s)
    recipes.offer(s, NOW)
    return recipes.get(rec["code"])


def test_nothing_runs_before_a_yes_and_only_the_owners_typed_words_count(held):
    rec = offered(held)
    code = rec["code"]
    for kw in (dict(chat="telegram:99"), dict(origin="test"), dict(origin="background"), dict(kind="voice"),
               dict(text="yes")):                                            # another chat, test origin, a voice note, no code
        args = dict(chat=OWNER, text=f"yes {code}", origin="user", kind="text") | kw
        assert recipes.preflight(args["chat"], args["text"], origin=args["origin"], kind=args["kind"],
                                 settings=held, tools=TOOLS) is None
    assert recipes.preflight(OWNER, "yes R999", origin="user", settings=held) is None       # not a code of ours
    assert routines._load() == {} and recipes.get(code)["status"] == "offered"


def test_yes_code_turns_it_into_a_read_only_routine(held):
    code = offered(held)["code"]
    reply = recipes.preflight(OWNER, f"Yes {code}", origin="user", settings=held, now=NOW, tools=TOOLS)
    assert "Routine recipe-school-check will run weekdays around 4:05 PM" in reply
    rt = list(routines._load().values())
    assert len(rt) == 1 and rt[0]["name"] == "recipe-school-check" and rt[0]["schedule"] == "weekdays 15:55"
    assert rt[0]["enabled"] and rt[0]["runs"] == 0 and rt[0]["deliver"] == "telegram"
    assert rt[0]["source"] == f"recipe:{code}"
    assert "google_classroom_due with days=3" in rt[0]["task"] and "Read only" in rt[0]["task"]
    assert recipes.get(code)["status"] == "accepted"
    assert outreach_ev.odds("recipe")["welcome"] == 1                        # a yes is welcome
    # "no CODE" later removes the routine again
    assert "Dropped" in recipes.preflight(OWNER, f"no {code}", origin="user", settings=held, now=NOW)
    assert routines._load() == {} and recipes.get(code)["status"] == "declined"


def test_a_yes_to_a_recipe_whose_tool_vanished_makes_nothing(held):
    code = offered(held)["code"]
    reply = recipes.preflight(OWNER, f"yes {code}", origin="user", settings=held, tools={"remind_me"})
    assert "not available" in reply and routines._load() == {}


def test_no_and_never_teach_the_welcome_odds(held):
    code = offered(held)["code"]
    assert "won't raise it again" in recipes.preflight(OWNER, f"no {code}", origin="user", settings=held, now=NOW)
    o1 = outreach_ev.odds("recipe", NOW)
    assert o1["unwelcome"] == 1 and o1["p"] < 0.5
    assert recipes.get(code)["status"] == "declined"
    assert recipes.offer(held, NOW + 2 * 86400).get("offered") is None      # a declined recipe is not offered again
    recipes._set(code, status="offered", offered_at=NOW)
    assert "dropped for good" in recipes.preflight(OWNER, f"never {code}", origin="user", settings=held, now=NOW)
    assert recipes.get(code)["status"] == "rejected" and outreach_ev.odds("recipe", NOW)["p"] < o1["p"]
    recipes.refresh(held, NOW + 60 * 86400, school_rows(), TOOLS, FakeChat(GOOD))
    assert recipes.get(code)["status"] == "rejected"                        # never means never


def test_a_bare_no_only_answers_the_one_suggestion_that_just_arrived(held, monkeypatch):
    code = offered(held)["code"]
    assert recipes.preflight(OWNER, "no", origin="user", settings=held, now=NOW) is None      # not delivered yet
    monkeypatch.setattr(recipes, "_offer_sent_at", lambda rec: NOW)
    assert recipes.preflight(OWNER, "no thanks", origin="user", settings=held, now=NOW + 60)
    assert recipes.get(code)["status"] == "declined"
    assert recipes.preflight(OWNER, "no", origin="user", settings=held, now=NOW + 120) is None  # nothing open now
    assert recipes.preflight(OWNER, "no, the robot is not ready", origin="user", settings=held) is None


# ---- privacy ----------------------------------------------------------------------------------------------------

def test_raw_titles_and_urls_never_reach_the_miner_the_model_the_offer_or_the_routine(held, monkeypatch):
    """Through the real sensor: ActivityWatch events carry secrets in titles and URL paths."""
    from datetime import timezone
    SECRET_T, SECRET_U = "ALGEBRA-HW-SECRET-9931", "URLPATH-SECRET-5512"
    iso = lambda ts: dt.datetime.fromtimestamp(ts, timezone.utc).isoformat()  # noqa: E731
    win, web = [], []
    for d in SCHOOL_DAYS:
        for (app, host, _p, secs), t in zip((CLASSROOM, DOCS, CALC), (at(d, 16), at(d, 16, 4), at(d, 16, 9))):
            title = f"{SECRET_T} - {app}"
            win.append({"id": 1, "timestamp": iso(t), "duration": float(secs), "data": {"app": app + ".exe", "title": title}})
            if host:
                web.append({"id": 1, "timestamp": iso(t), "duration": float(secs),
                            "data": {"url": f"https://{host}/path/{SECRET_U}?q={SECRET_T}", "title": title}})

    class Aw:
        def __call__(self, path, params=None, timeout=4.0):
            if path == "/api/0/buckets/":
                return {"w": {"type": "currentwindow"}, "b": {"type": "web.tab.current"}}
            return win if "/w/" in path else web if "/b/" in path else []
    aw_sensor.poll(held, now=NOW, get=Aw(), labels={})
    rows = aw_sensor.slices_since(0)
    assert rows and not any(SECRET_T in repr(r) or SECRET_U in repr(r) for r in rows)
    monkeypatch.setattr(aw_sensor, "CATCH_UP_S", 40 * 86400)
    chat = FakeChat(GOOD)
    out = recipes.run(held, NOW, rows, TOOLS, chat)
    assert out["drafted"] == 1 and "offered" in out["offer"], out
    code = recipes.all_recipes()[0]["code"]
    recipes.preflight(OWNER, f"yes {code}", origin="user", settings=held, now=NOW, tools=TOOLS)
    blob = "".join(json.dumps(x, default=str) for x in (chat.calls, [dict(r) for r in outbox.list()], routines._load()))
    blob += json.dumps(recipes.all_recipes(), default=str) + recipes._show(recipes.get(code))
    blob += b"".join(p.read_bytes() for p in data_path("").iterdir() if p.is_file()).decode("utf-8", "replace")
    for secret in (SECRET_T, SECRET_U, "/path/"):
        assert secret not in blob, secret


# ---- reporting and command line ---------------------------------------------------------------------------------

def test_evening_line_and_summary_show_a_new_suggestion_until_it_is_answered(held):
    assert recipes.evening_line(NOW, held) == "" and recipes.summary(held)["open"] == []
    code = offered(held)["code"]
    line = recipes.evening_line(NOW + 3600, held)
    assert code in line and "weekdays around 4:05 PM" in line
    assert recipes.summary(held)["open"][0]["code"] == code and recipes.summary(held)["counts"] == {"offered": 1}
    assert recipes.evening_line(NOW + 2 * 86400, held) == ""               # a day later it is not "new"
    from waggle import brief
    assert f"yes {code}" in brief.evening_note(held, now=NOW + 3600, activity={})


def test_command_line(held, capsys, monkeypatch):
    monkeypatch.setattr(recipes, "available_tools", lambda: TOOLS)
    assert recipes.main(["list"]) == 0 and "no recipes yet" in capsys.readouterr().out
    rec = ready_recipe(held)
    code = rec["code"]
    monkeypatch.setattr(recipes, "_rows", lambda now, rows=None: school_rows())
    assert recipes.main(["list"]) == 0
    assert code in capsys.readouterr().out
    assert recipes.main(["show", code]) == 0
    shown = capsys.readouterr().out
    assert "trigger: weekdays around 4:05 PM" in shown and "nothing was run" in shown and "draft (model)" in shown
    assert recipes.main(["shadow", code]) == 0 and "shadow:" in capsys.readouterr().out
    assert routines._load() == {}
    assert recipes.main(["accept", code]) == 0 and "Routine recipe-school-check" in capsys.readouterr().out
    assert recipes.main(["reject", code, "--never"]) == 0
    assert recipes.get(code)["status"] == "rejected" and routines._load() == {}
    assert recipes.main(["show", "RZZZ"]) == 1 and recipes.main(["bogus"]) == 2


def test_the_core_job_and_timer_register(monkeypatch):
    from waggle.core import jobs, timers
    out = recipes.register_core()
    assert out == {"jobs": ["recipes.run"], "timers": ["recipes.run"]}
    assert jobs.handler("recipes.run").detached
    assert "waggle.recipes" in __import__("waggle.core.plugins", fromlist=["x"]).MODULES
    assert recipes.register_core() == out                                   # at every start, no change


def test_the_engine_answers_yes_code_without_a_model(held, monkeypatch):
    from waggle import traces
    monkeypatch.setattr(recipes, "available_tools", lambda: TOOLS)
    held.edit_interval_s = 0.05
    code = offered(held)["code"]
    clock = FakeClock()
    api = FakeBotApi(clock=clock)
    chat_engine.set_delivery_factory("telegram", lambda address: BotApiDelivery(address, api, clock=clock, sleep=clock.sleep))
    fake = FakeClaude(None)
    sess = ChatSession("telegram:42", settings=held, popen=fake, claude="claude")
    rec = chat_engine.handle_inbound("telegram:42", f"yes {code}", settings=held, session=sess)
    assert fake.procs == [] and rec.answered_by == "none" and rec.delivered
    assert "Routine recipe-school-check" in api.messages[rec.message_ids[0]]
    assert traces.get(rec.trace_id)["route"] == "recipe" and len(routines._load()) == 1
