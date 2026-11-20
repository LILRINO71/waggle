"""Outreach on the core: transports registered with the core sender, readiness from the ledger, timing through
core timers and jobs. Every transport here is a fake: nothing reaches a real phone, ntfy or Twilio."""
import datetime as dt
import io
import json
import sys
import types
import urllib.error

import pytest

from waggle import call, notify, tick
from waggle.config import Settings
from waggle.core import outbox


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def rows():
    return sorted(outbox.list(limit=1000), key=lambda r: r["id"])


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    return Settings.load()


@pytest.fixture
def fake_sender(monkeypatch):
    """A stand-in for waggle.core.sender (being written separately): it only records registrations."""
    registered = {}
    mod = types.ModuleType("waggle.core.sender")
    mod.register = lambda kind, fn: registered.__setitem__(kind, fn)
    monkeypatch.setitem(sys.modules, "waggle.core.sender", mod)
    return registered


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(b'{"message": "no"}'))


# ---- registration ---------------------------------------------------------------------------------------

def test_register_core_registers_push_and_call_with_the_sender(s, fake_sender):
    report = tick.register_core()
    assert set(fake_sender) >= {"push", "call"} and "text" not in fake_sender
    assert fake_sender["push"] is notify.push_sender and fake_sender["call"] is call.call_sender
    assert report["senders"] == ["push", "call"]


def test_register_core_without_the_sender_module_still_starts(s, monkeypatch):
    monkeypatch.setitem(sys.modules, "waggle.core.sender", None)  # import fails
    report = tick.register_core()
    assert "sender_error" in report


# ---- push ------------------------------------------------------------------------------------------------

def _push_row(urgent=True):
    oid = notify.deliver("push", "meet in 20 min", title="Waggle", urgent=urgent, origin="initiative")["id"]
    return outbox.get(oid)


def test_push_sender_returns_the_ntfy_id(s):
    seen = []
    mid = notify.push_sender(_push_row(), post=lambda url, body: seen.append(body) or {"id": "abc123"})
    assert mid == "abc123" and seen[0]["priority"] == 5 and seen[0]["message"] == "meet in 20 min"


def test_push_sender_failures_say_whether_to_retry(s):
    def refuse(code):
        def post(url, body):
            raise http_error(code)
        return post

    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=refuse(400))
    assert e.value.permanent
    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=refuse(429))
    assert not e.value.permanent and e.value.retry_after == 60
    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=lambda url, body: (_ for _ in ()).throw(TimeoutError("slow")))
    assert not e.value.permanent and not hasattr(e.value, "retry_after")


# ---- call ------------------------------------------------------------------------------------------------

@pytest.fixture
def twilio(tmp_path, monkeypatch):
    env = tmp_path / "t.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)
    state = {"provisioned": True, "post": lambda req: Resp(json.dumps({"sid": "CA9"}).encode()), "posts": 0}
    monkeypatch.setattr(call, "provisioned", lambda refresh=False, opener=None, now=None: {
        "ready": state["provisioned"], "missing": "" if state["provisioned"] else call.STEP_TO})

    def opener(req, timeout):
        state["posts"] += 1
        return state["post"](req)
    state["opener"] = opener
    return state


def _call_row(now, **kw):
    oid = notify.deliver("call", "the build broke", urgent=True, origin="initiative", settings=Settings.load(),
                         now=now, **kw)["id"]
    return outbox.get(oid)


def test_call_sender_places_the_call_and_returns_the_sid(s, twilio):
    row = _call_row(at(12))
    assert call.call_sender(row, opener=twilio["opener"], now=at(12, 1)) == "CA9"
    assert twilio["posts"] == 1 and [r["kind"] for r in rows()] == ["call"]  # no fallback
    assert notify.readiness("call")["state"] != "verified"  # a placed call is not yet a call that rang


def test_call_sender_provider_failure_retries_then_falls_back(s, twilio):
    def boom(req):
        raise http_error(500)
    twilio["post"] = boom
    row = _call_row(at(12))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 1))
    assert not e.value.permanent and e.value.retry_after == 30 and len(rows()) == 1
    row["attempts"] = call.CALL_MAX_ATTEMPTS  # the last attempt also fails
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 2))
    assert e.value.permanent
    kinds = [(r["kind"], r["meta"]["urgent"]) for r in rows()[1:]]
    assert kinds == [("push", True), ("text", True)] and "Call not possible" in rows()[2]["body"]
    assert notify.readiness("call")["state"] == "failing"


def test_call_sender_refused_request_is_permanent(s, twilio):
    def refused(req):
        raise http_error(400)
    twilio["post"] = refused
    with pytest.raises(notify.SendError) as e:
        call.call_sender(_call_row(at(12)), opener=twilio["opener"], now=at(12, 1))
    assert e.value.permanent and [r["kind"] for r in rows()] == ["call", "push", "text"]


def test_call_sender_not_provisioned_never_dials(s, twilio):
    twilio["provisioned"] = False
    row = _call_row(at(12))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 1))
    assert e.value.permanent and twilio["posts"] == 0
    assert [r["kind"] for r in rows()] == ["call", "push", "text"]
    with pytest.raises(notify.SendError):  # a retried sender does not double the fallback
        call.call_sender(row, opener=twilio["opener"], now=at(12, 2))
    assert len(rows()) == 3


def test_late_call_becomes_a_text_that_says_so(s, twilio):
    row = _call_row(at(7))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(9))
    assert e.value.permanent and twilio["posts"] == 0
    late = rows()[1]
    assert late["kind"] == "text" and "late: the PC was off" in late["body"] and "07:00" in late["body"]


# ---- readiness only from evidence ----------------------------------------------------------------------------

def test_readiness_is_the_ledger(s):
    from waggle.core import ledger
    assert notify.readiness("telegram_text") == {"channel": "telegram_text", "state": "not verified", "ready": False,
                                                 "line": "never delivered end to end"}
    notify.record_evidence("telegram_text", True, "message id 42 in the user's chat", ref="tg:42", now=at(10))
    assert ledger.ready("telegram_text", now=at(11)) and notify.readiness("telegram_text", now=at(11))["ready"]
    assert not notify.readiness("telegram_text", now=at(10) + 3 * 86400)["ready"]  # older than its time limit
    notify.record_evidence("telegram_text", False, "HTTP 502", now=at(12))
    assert notify.readiness("telegram_text", now=at(13))["state"] == "failing"
    assert notify.ledger_name("ntfy") == "ntfy.accepted"  # a post the server took is the weaker fact


def test_a_provisioned_twilio_account_never_makes_calls_ready(s, tmp_path, monkeypatch):
    from waggle.core import ledger
    env = tmp_path / "t.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)

    def opener(req, timeout):
        url = req.full_url
        if "IncomingPhoneNumbers" in url:
            return Resp(json.dumps({"incoming_phone_numbers": [{"sid": "PN1"}]}).encode())
        if "OutgoingCallerIds" in url:
            return Resp(json.dumps({"outgoing_caller_ids": [{"sid": "CI1"}]}).encode())
        if "/Calls/" in url:
            return Resp(json.dumps({"status": "completed", "duration": "14"}).encode())
        return Resp(json.dumps({"status": "active", "type": "Trial"}).encode())
    assert call.provisioned(refresh=True, opener=opener)["ready"] and call.configured()
    assert ledger.ready("call.provisioned") and not ledger.ready("call")
    assert notify.readiness("call")["state"] == "not verified"
    st = call._load_status()
    st["pending"] = [{"sid": "CA9", "time": at(12)}]
    call._save_status(st)
    assert call.check_pending(opener=opener) == [("CA9", "completed")]
    assert notify.readiness("call")["state"] == "verified" and "completed" in notify.readiness("call")["line"]


# ---- timing through core timers and jobs ---------------------------------------------------------------------

def run_jobs(kind, now):
    from waggle.core import jobs
    out = []
    while True:
        j = jobs.run_one(kinds=[kind], now=now)
        if j is None:
            return out
        out.append(j)


def test_register_core_adds_its_timers_once(s, fake_sender):
    from waggle.core import timers
    tick.register_core(now=at(12))
    first = {t["id"]: t for t in timers.list()}
    assert set(first) == {"outreach.look", "outreach.brief", "outreach.evening", "outreach.scout"}
    assert first["outreach.look"]["catch_up"] == "skip" and first["outreach.look"]["kind"] == "interval"
    ev = first["outreach.evening"]
    assert (ev["kind"], ev["spec"], ev["catch_up"], ev["max_late_s"]) == ("cron", "0 21 * * *", "latest",
                                                                          s.evening_catchup_hours * 3600)
    assert first["outreach.brief"]["spec"] == "30 7 * * *" and first["outreach.brief"]["catch_up"] == "latest"
    tick.register_core(now=at(13))  # the next start: same schedule, nothing duplicated
    again = {t["id"]: t for t in timers.list()}
    assert {k: v["next_run"] for k, v in again.items()} == {k: v["next_run"] for k, v in first.items()}


def test_evening_note_caught_up_late_once_or_dropped(s, fake_sender):
    from waggle.core import timers
    tick.register_core(now=at(12))
    fired = timers.fire_due(at(21, 40))  # the PC came on 40 min after 21:00: still inside evening_catchup
    assert len(fired) >= 1
    done = run_jobs("outreach.evening", at(21, 40))
    assert len(done) == 1 and done[0]["state"] == "done" and done[0]["payload"]["late_s"] == 40 * 60
    note = [r for r in rows() if r["origin"] == "evening"]
    assert len(note) == 1 and note[0]["idem_key"] == "evening:2026-11-19" and note[0]["body"].startswith("🌙")
    report = timers.catch_up(at(23, 30, day=20))  # off all evening the next day: 2.5 h late, dropped
    assert any(x["timer_id"] == "outreach.evening" for x in report["skipped"])
    assert run_jobs("outreach.evening", at(23, 30, day=20)) == []


def test_reminder_that_fires_late_says_so(s):
    from waggle import followups
    from waggle.core import timers
    tick.register_jobs()
    fid = followups.add(at(7, day=20), "Charge the batteries", now=at(12))
    timers.catch_up(at(9, 30, day=20))  # the PC was off at 07:00
    done = run_jobs("outreach.reminder", at(9, 30, day=20))
    assert len(done) == 1 and done[0]["payload"]["late_s"] == 2.5 * 3600
    body = rows()[0]["body"]
    assert body.startswith("⏰ Charge the batteries") and "late: the PC was off at 07:00" in body
    assert followups.get(fid)["status"] == "sent"


def test_reminders_from_tests_or_agents_never_reach_the_phone(s):
    from waggle import followups
    from waggle.core import timers
    tick.register_jobs()
    followups.add(at(13), "bench reminder", origin="bench", now=at(12))
    timers.fire_due(at(13))
    run_jobs("outreach.reminder", at(13))
    assert rows() == []


def test_scheduled_call_on_time_queues_a_call_and_late_becomes_a_text(s):
    from waggle.core import timers
    tick.register_jobs()
    call.schedule_at(at(13), "the meet starts soon")
    call.schedule_at(at(7, day=20), "wake up for the meet")
    timers.fire_due(at(13))
    run_jobs("outreach.call", at(13))
    timers.catch_up(at(9, day=20))  # PC off at 07:00
    run_jobs("outreach.call", at(9, day=20))
    got = [(r["kind"], r["origin"], r["body"]) for r in rows()]
    assert got[0] == ("call", "asked", "the meet starts soon")
    assert got[1][0] == "text" and "late: the PC was off, so I didn't call at 07:00" in got[1][2]
    assert len(got) == 2


def test_no_json_state_files_are_written(s, fake_sender):
    from waggle.config import data_path
    from waggle.core import timers
    from waggle import followups
    tick.register_core(now=at(12))
    followups.add(at(13), "x", now=at(12))
    tick.look(settings=s, now=at(12), activity={}, asker=lambda *a, **k: {"json": {"decisions": []}})
    call.provisioned()
    timers.fire_due(at(13))
    run_jobs("outreach.reminder", at(13))
    leftovers = {"outbox.db", "channel_evidence.json", "followups.json", "tick_state.json", "watchers_state.json",
                 "scout_state.json", "call_status.json", "scheduled_calls.json"}
    assert not [n for n in leftovers if data_path(n).exists()]


# ---- origin rule (decision 10) ---------------------------------------------------------------------------------

@pytest.fixture
def mixed_turns(tmp_path, monkeypatch):
    """Turns from the user's own Telegram chat mixed with a CLI prompt, an agent session, a bench run and a
    Telegram-platform session that Hermes says a one-shot started."""
    import sqlite3

    from waggle import origin
    db = origin.state_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT)")
    con.execute("CREATE TABLE messages (session_id TEXT, role TEXT, timestamp REAL)")
    con.execute("INSERT INTO sessions VALUES ('tg1', 'telegram'), ('dev1', 'oneshot')")
    con.commit()
    con.close()
    t = at(10)
    return [
        {"id": 1, "time": t, "platform": "telegram", "session": "tg1", "request": "I'll finish the essay tonight"},
        {"id": 2, "time": t + 1, "platform": "cli", "session": "c1", "request": "I'll benchmark this tonight"},
        {"id": 3, "time": t + 2, "platform": "telegram", "session": "objective-ab12", "request": "I'll plan it tonight"},
        {"id": 4, "time": t + 3, "platform": "telegram", "session": "dev1", "request": "I'll test the meet tonight"},
        {"id": 5, "time": t + 4, "platform": "telegram", "session": "tg1", "request": "I'll ship it tonight",
         "extra": '{"origin": "bench"}'},
    ]


def test_only_the_users_own_turns_count(s, mixed_turns):
    from waggle import origin
    assert [r["id"] for r in origin.user_turns(mixed_turns, s)] == [1]


def test_followups_capture_reads_only_user_turns(s, mixed_turns):
    from waggle import followups
    added = followups.capture(s, at(10, 5), rows=mixed_turns)
    assert [a["what"] for a in added] == ["finish the essay"]


def test_decision_context_and_scout_exclude_machine_turns(s, mixed_turns, monkeypatch):
    from waggle import initiative, scout, traces
    monkeypatch.setattr(traces, "all_traces", lambda since=0.0, limit=5000: list(reversed(mixed_turns)))
    ctx = initiative.context(s, at(11))
    assert "finish the essay" in ctx
    assert not any(w in ctx for w in ("benchmark", "plan it", "test the meet", "ship it"))
    many = [dict(mixed_turns[3], id=10 + d, time=at(10, day=10 + d)) for d in range(4)]  # a dev prompt on 4 days
    assert scout.daily(s, at(18), rows=many, commits={}) == []


def test_replies_from_the_cli_do_not_count_as_the_user_answering(s):
    from waggle import initiative
    from waggle.core import outbox
    oid = notify.deliver("text", "build broke", origin="initiative", settings=s, now=at(12))["id"]
    outbox.claim_due(now=at(12))
    outbox.mark_sent(oid, "tg:7", now=at(12))
    assert initiative.track_responses(s, at(13), turns=[(at(12, 30), "cli")]) == 0
    assert initiative.track_responses(s, at(13), turns=[(at(12, 31), "telegram")]) == 1


def test_every_outbox_row_carries_its_origin(s):
    from waggle import initiative
    initiative.record([{"key": "pr", "kind": "github_pr", "title": "PR waiting", "topic": "pr", "urgent": False}],
                      at(12))
    initiative.decide(s, at(12), asker=lambda *a, **k: {"json": {"decisions": [
        {"key": "pr", "action": "text", "message": "PR #3 has waited 3 days.", "reason": "people wait"}]}})
    notify.send("objective done", settings=s)
    assert [r["origin"] for r in rows()] == ["initiative", "notify"]
    assert all(r["idem_key"].startswith(r["origin"] + ":") for r in rows())
