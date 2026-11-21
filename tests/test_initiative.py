"""Initiative rebuild (work in progress): outbox, evidence, guard rails, follow-ups, one-shots, dev filter, calls."""
import datetime as dt
import io
import json
import sqlite3

import pytest

from waggle import call, followups, initiative, notify, origin, tick, watchers
from waggle.config import Settings


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    return Settings.load()


def _outbox():
    from waggle.core import outbox
    return sorted(outbox.list(limit=1000), key=lambda r: r["id"])


def test_deliver_writes_exactly_one_outbox_row_and_sends_nothing(s):
    row = notify.deliver("text", "build finished", origin="initiative", decision_id="d1", topic="ci:x",
                         settings=s, now=at(10))
    again = notify.deliver("text", "build finished", origin="initiative", decision_id="d1", settings=s, now=at(10, 1))
    rows = _outbox()
    assert row["id"] == again["id"] and len(rows) == 1 and notify.PHONE_LOG.direct == []
    r = rows[0]
    assert r["state"] == "queued" and r["kind"] == "text" and r["origin"] == "initiative"
    assert r["idem_key"] == "initiative:d1:text" and r["not_before"] is None
    assert r["meta"]["topic"] == "ci:x" and r["meta"]["decision_id"] == "d1" and r["meta"]["urgent"] is False
    assert r["expires"] == at(10) + s.outbox_max_age_h * 3600


def test_quiet_hours_hold_non_urgent_rows_until_morning(s):
    held = notify.deliver("text", "one", settings=s, now=at(23, 30))
    urgent = notify.deliver("text", "two", urgent=True, settings=s, now=at(23, 40))
    asked = notify.deliver("text", "three", asked=True, settings=s, now=at(23, 41))
    push = notify.deliver("push", "four", settings=s, now=at(23, 42))
    assert held["held"] and held["not_before"] == at(7, day=20)
    assert held["expires"] == at(7, day=20) + s.outbox_max_age_h * 3600
    assert [r["not_before"] for r in (urgent, asked, push)] == [None, None, None]
    assert urgent["priority"] > asked["priority"] > held["priority"]


def test_readiness_comes_from_evidence(s):
    assert notify.readiness("telegram_text")["state"] == "not verified"
    notify.record_evidence("ntfy", True, "server accepted")
    assert notify.readiness("ntfy")["state"] == "not verified"  # needs the user's confirmation
    notify.confirm_channel("ntfy")
    assert notify.readiness("ntfy")["state"] == "verified"
    notify.record_evidence("ntfy", False, "post failed", now=9e9)
    assert notify.readiness("ntfy")["state"] == "failing"


def test_developer_activity_never_reaches_the_phone(s):
    before = len(notify.PHONE_LOG)
    notify.send("Objective done: M3 test", source="oneshot", settings=s)
    assert len(notify.PHONE_LOG) == before
    db = s.vault + "_state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT)")
    con.execute("CREATE TABLE messages (session_id TEXT, role TEXT, timestamp REAL)")
    con.execute("INSERT INTO sessions VALUES ('a', 'oneshot'), ('b', 'telegram')")
    con.execute("INSERT INTO messages VALUES ('a', 'user', 100), ('b', 'user', 1000)")
    con.commit()
    con.close()
    assert origin.source_at(101, db=db) == "oneshot" and origin.source_at(1001, db=db) == "telegram"
    assert origin.source_at(5000, db=db) == ""


def _sig(key, urgent=False, kind="github_ci", topic=None):
    return {"key": key, "kind": kind, "title": f"{key} happened", "detail": "", "topic": topic or key,
            "urgent": urgent}


def test_guard_rails_hold_whatever_the_model_says(s):
    s.daily_text_cap = 2
    s.save()
    initiative.record([_sig(f"ci{i}") for i in range(4)] + [_sig("same-topic", topic="ci0")]
                      + [_sig("meet", urgent=True, kind="upcoming")], at(12))
    loud = lambda prompt, **k: {"json": {"decisions": [{"key": k_, "action": "text", "message": f"msg {k_}",
                                                        "reason": "model wants it"} for k_ in
                                                       ["ci0", "ci1", "ci2", "ci3", "same-topic", "meet"]]}}
    out = {d["signal"]: d["action"] for d in initiative.decide(s, at(12), asker=loud)}
    assert out["meet"] == "text"  # urgent bypasses the cap
    assert sum(1 for k in ("ci0", "ci1", "ci2", "ci3") if out[k] == "text") == 2  # the cap
    assert out["same-topic"] == "none"  # topic already raised
    rows = initiative.decisions()
    assert all(r["reason"] for r in rows) and any("cap" in r["guard"] for r in rows)
    initiative.record([_sig("night")], at(23, 30))
    night = initiative.decide(s, at(23, 30), asker=loud)
    assert night[0]["action"] == "evening"  # quiet hours


def test_urgent_fact_goes_out_when_claude_is_down(s):
    initiative.record([_sig("soon", urgent=True, kind="upcoming"), _sig("later")], at(12))

    def down(*a, **k):
        raise RuntimeError("quota")
    out = initiative.decide(s, at(12), asker=down)
    assert [(d["signal"], d["action"]) for d in out] == [("soon", "text")]
    assert "soon happened" in _outbox()[0]["body"] and _outbox()[0]["origin"] == "initiative"
    assert not _outbox()[0]["body"].startswith("🐝 🐝")  # the rules fallback once doubled the bee
    assert [p["key"] for p in initiative.pending()] == ["later"]  # waits for the next run


def test_commitment_becomes_a_dated_checkin(s):
    said = dt.datetime(2026, 11, 19, 16, 0)
    got = followups.extract("ok cool. I'll test the shooter tomorrow after practice.", said)
    assert got and got[0][0] == "checkin" and "test the shooter" in got[0][1]
    assert dt.datetime.fromtimestamp(got[0][2]) == dt.datetime(2026, 11, 20, 19, 0)
    assert followups.extract("what if I tested it tomorrow?", said) == []
    rows = [{"id": 1, "time": said.timestamp(), "platform": "telegram",
             "request": "I'll test the shooter tomorrow"},
            {"id": 2, "time": said.timestamp(), "platform": "cli", "request": "I'll benchmark this tomorrow"}]
    added = followups.capture(s, said.timestamp(), rows=rows)
    assert len(added) == 1 and added[0]["kind"] == "checkin" and added[0]["timer_id"] == f"checkin:{added[0]['id']}"
    assert followups.capture(s, said.timestamp(), rows=rows) == []  # the cursor moved on
    from waggle.core import jobs, timers
    tick.register_jobs()
    assert timers.fire_due(at(18, day=20)) == []
    assert len(timers.fire_due(at(19, 1, day=20))) == 1
    assert jobs.run_one(kinds=["outreach.checkin"], now=at(19, 1, day=20))["state"] == "done"
    due = initiative.pending()
    assert len(due) == 1 and due[0]["kind"] == "followup" and "test the shooter" in due[0]["title"]


def test_one_shot_reminder_fires_once(s):
    from waggle.core import jobs, timers
    tick.register_jobs()
    fid = followups.add(at(19, day=20), "Charge the batteries", now=at(12))
    assert timers.fire_due(at(18, day=20)) == []
    assert len(timers.fire_due(at(19, 0, day=20) + 30)) == 1
    assert timers.fire_due(at(19, 2, day=20)) == []  # once
    assert jobs.run_one(kinds=["outreach.reminder"], now=at(19, 1, day=20))["state"] == "done"
    assert [r["body"] for r in _outbox()] == ["⏰ Charge the batteries"]
    assert _outbox()[0]["origin"] == "reminder" and followups.get(fid)["status"] == "sent"


def test_call_me_at_a_time():
    now = dt.datetime(2026, 11, 19, 16, 0)
    mins, about = call.parse_request("call me at 7 tomorrow about the meet", now.timestamp())
    assert (now + dt.timedelta(minutes=mins)) == dt.datetime(2026, 11, 20, 7, 0) and about == "the meet"
    assert call.parse_request("call me in 10 minutes about the build") == (10.0, "the build")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_configured_is_false_without_a_number(s, monkeypatch, tmp_path):
    monkeypatch.setattr(call, "enabled", lambda settings=None: True)  # calls are off by default
    assert call.configured() is False and "Setup Calls" in call.provisioned()["missing"]
    env = tmp_path / "t.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)

    def opener(req, timeout):
        url = req.full_url
        if "IncomingPhoneNumbers" in url:
            return Resp(json.dumps({"incoming_phone_numbers": []}).encode())
        return Resp(json.dumps({"status": "active", "type": "Trial"}).encode())
    p = call.provisioned(refresh=True, opener=opener)
    assert p["ready"] is False and p["missing"] == call.STEP_FROM


def test_evening_note_always_has_content(s):
    text = __import__("waggle.brief", fromlist=["x"]).evening_note(s, at(21), activity={})
    assert text.startswith("🌙") and "Tomorrow:" in text and "Did:" in text


def test_upcoming_and_github_watchers(s):
    note = ("# Upcoming\n- 2026-11-19 14:30 Math 1402 unit test\n- 2026-11-21 FTC meet\n"
            "## Mail to answer\n- Coach asked about the meet roster\n")
    sigs = watchers.upcoming_watch(s, at(12), text=note)
    by = {x["kind"]: x for x in sigs}
    assert any(x["urgent"] and "Math" in x["title"] for x in sigs) and "mail" in by
    runs = [[{"databaseId": 1, "workflowName": "CI", "headBranch": "main", "status": "completed",
              "conclusion": "success", "url": "u", "createdAt": "2026-11-19T10:00:00Z"}],
            [{"databaseId": 2, "workflowName": "CI", "headBranch": "main", "status": "completed",
              "conclusion": "failure", "url": "u", "createdAt": "2026-11-19T11:00:00Z"}]]

    def gh(args):
        if args[0] == "run":
            return runs[0]
        return []
    s.github_repos = ["someone/robot"]
    assert watchers.github_watch(s, gh=gh, now=at(12)) == []  # baseline
    runs.pop(0)
    got = watchers.github_watch(s, gh=gh, now=at(13))
    assert len(got) == 1 and "failed on main" in got[0]["title"]
