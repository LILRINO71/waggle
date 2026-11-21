"""Holding unprompted messages for the owner's next natural break (waggle/holdq.py). Fakes only: a fake presence
reading, fake break signals, a fake transport; no real repos, sessions or PC."""
import datetime as dt
import json

import pytest

from waggle import holdq, notify
from waggle.config import Settings
from waggle.core import inbound, outbox, sender, store


def at(h, m=0):
    return dt.datetime(2026, 11, 20, h, m).timestamp()


AT_PC = {"at_pc": True, "idle_s": 5.0, "locked": False}
AWAY = {"at_pc": False, "idle_s": 900.0, "locked": False}


@pytest.fixture
def s(settings, monkeypatch):
    settings.hold_for_breaks = True
    settings.quiet_hours = [23, 7]
    settings.save()
    monkeypatch.setattr(holdq, "_presence_state", lambda: AT_PC)
    monkeypatch.setattr(holdq, "_last_read", 0.0)
    from waggle import activity
    monkeypatch.setattr(holdq, "_repos", lambda st, now: {})                    # never the real repos or sessions
    monkeypatch.setattr(activity, "scan_sessions", lambda st, now=None: [])
    return Settings.load()


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.close_all()


def suggest(s, now, text="your CI is red", origin="initiative", **kw):
    return notify.deliver("text", text, origin=origin, ref=text, settings=s, now=now, **kw)


def brk(kind, ts, id_=None, lookback=0.0):
    return {"type": kind, "ts": ts, "id": id_ or f"{kind}:{ts}", "lookback": lookback}


def passes(s, now, breaks=(), presence=AT_PC):
    return holdq.release_pass(now, s, signals=lambda since, now_, settings: list(breaks), presence_state=presence)


# ---- what is held, what never is -----------------------------------------------------------------------------

def test_an_unprompted_suggestion_is_held_not_queued(s):
    row = suggest(s, at(14))
    assert row["status"] == "held" and row["held"] and row["hold"]["max_wait_s"] == 90 * 60
    assert row["hold"]["held_at"] == at(14) and row["hold"]["max_wait_at"] == at(14) + 90 * 60
    assert outbox.claim_due(now=at(15)) == []          # the sender cannot claim it
    assert [r["id"] for r in outbox.held()] == [row["id"]]


@pytest.mark.parametrize("kw", [
    dict(origin="reminder", asked=True),                      # a reminder the owner set
    dict(origin="calendar", asked=True),                      # a calendar heads-up
    dict(origin="mailwatch"),                                 # a mail alert
    dict(origin="brief", asked=True), dict(origin="evening", asked=True),
    dict(origin="proposal"), dict(origin="background"), dict(origin="canary"),
    dict(origin="initiative", urgent=True),                   # urgent
    dict(origin="initiative", asked=True),                    # the owner asked for it
])
def test_these_are_never_held(s, kw):
    row = suggest(s, at(14), **kw)
    assert row["status"] == "queued" and not row["held"]
    assert outbox.claim_due(now=at(14))[0]["id"] == row["id"]


def test_pushes_and_calls_are_never_held(s):
    assert notify.deliver("push", "x", origin="initiative", settings=s, now=at(14))["status"] == "queued"
    assert notify.deliver("call", "x", origin="initiative", settings=s, now=at(14))["status"] == "queued"


def test_followups_scouts_and_routine_results_are_held(s):
    for origin in ("followup", "scout", "routine"):
        assert suggest(s, at(14), text=origin, origin=origin)["status"] == "held"


def test_a_voice_suggestion_is_held_too(s):
    row = notify.voice("tests are red", origin="initiative", settings=s, now=at(14), synth=lambda t, o, st: o.write_bytes(b"x") or True)
    assert row["kind"] == "voice" and row["status"] == "held"


def test_the_switch_off_restores_the_old_behaviour(s):
    s.hold_for_breaks = False
    assert suggest(s, at(14))["status"] == "queued"


def test_quiet_hours_still_hold_it_until_morning_as_before(s):
    row = suggest(s, at(23, 30))
    assert row["status"] == "queued" and row["not_before"] == at(7) + 86400 and row["hold"] is None


# ---- the release triggers --------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["owner_message", "commit", "push", "session_end", "pc_return"])
def test_each_break_releases_the_held_row_and_says_why(s, kind):
    row = suggest(s, at(14))
    if kind == "pc_return":
        assert passes(s, at(14, 10), presence=AWAY) == []                 # owner leaves: nothing yet
        got = passes(s, at(14, 20), presence=AT_PC)                       # owner comes back
    else:
        assert passes(s, at(14, 5)) == []                                 # no break yet
        got = passes(s, at(14, 20), [brk(kind, at(14, 15))])
    assert [(g["id"], g["by"]) for g in got] == [(row["id"], kind)]
    r = outbox.get(row["id"])
    assert r["state"] == "queued" and r["not_before"] is None
    assert r["meta"]["hold"]["released_by"] == kind and r["meta"]["hold"]["waited_s"] == 20 * 60
    assert outbox.claim_due(now=at(14, 21))[0]["id"] == row["id"]          # and now the sender can send it
    ev = [e for e in store.connect().execute("SELECT data FROM events WHERE kind='outbox.released'")]
    assert json.loads(ev[-1][0])["by"] == kind


def test_a_break_from_before_the_hold_does_not_release_it(s):
    suggest(s, at(14, 10))
    assert passes(s, at(14, 20), [brk("commit", at(14, 5))]) == []


def test_a_session_that_ended_just_before_the_hold_counts_through_its_lookback(s):
    suggest(s, at(14, 10))
    got = passes(s, at(14, 20), [brk("session_end", at(14, 7), lookback=holdq.SESSION_LOOKBACK_S)])
    assert [g["by"] for g in got] == ["session_end"]


def test_one_row_per_break_the_next_waits_for_the_next_one(s):
    a = suggest(s, at(14), text="a")
    b = suggest(s, at(14, 1), text="b")
    first = passes(s, at(14, 20), [brk("commit", at(14, 15))])
    assert [g["id"] for g in first] == [a["id"]] and outbox.get(b["id"])["state"] == "held"
    again = passes(s, at(14, 21), [brk("commit", at(14, 15))])               # the same commit is not used twice
    assert again == []
    second = passes(s, at(14, 40), [brk("commit", at(14, 15)), brk("push", at(14, 30))])
    assert [(g["id"], g["by"]) for g in second] == [(b["id"], "push")]


def test_max_wait_sends_it_anyway_and_says_so(s):
    row = suggest(s, at(14))
    assert passes(s, at(14, 0) + 89 * 60) == []
    got = passes(s, at(14, 0) + 90 * 60)
    assert [(g["id"], g["by"]) for g in got] == [(row["id"], "max_wait")]
    assert outbox.get(row["id"])["meta"]["hold"]["released_by"] == "max_wait"


def test_every_row_past_its_max_wait_goes_not_just_one(s):
    rows = [suggest(s, at(14), text=str(i)) for i in range(3)]
    got = passes(s, at(14) + 91 * 60)
    assert sorted(g["id"] for g in got) == sorted(r["id"] for r in rows)


def test_the_max_wait_comes_from_the_setting(s):
    s.hold_max_wait_min = 20
    row = suggest(s, at(14))
    assert row["hold"]["max_wait_at"] == at(14) + 20 * 60
    assert [g["by"] for g in passes(s, at(14, 21))] == ["max_wait"]


def test_a_release_at_night_still_waits_for_the_morning(s):
    row = suggest(s, at(22, 30))
    assert row["status"] == "held"
    got = passes(s, at(23, 40), [brk("owner_message", at(23, 35))])
    assert got and outbox.get(row["id"])["not_before"] == at(7) + 86400
    assert outbox.claim_due(now=at(23, 41)) == []


def test_away_is_only_set_when_the_owner_was_seen_away_so_staying_at_the_pc_is_no_break(s):
    row = suggest(s, at(14))
    assert passes(s, at(14, 5), presence=AT_PC) == [] and passes(s, at(14, 10), presence=AT_PC) == []
    assert outbox.get(row["id"])["state"] == "held"


def test_a_row_held_while_away_is_released_when_the_owner_returns(s, monkeypatch):
    monkeypatch.setattr(holdq, "_presence_state", lambda: AWAY)       # held while the PC is locked or idle
    row = suggest(s, at(14))
    assert row["hold"]["away"] is True
    got = passes(s, at(14, 3), presence=AT_PC)
    assert [g["by"] for g in got] == ["pc_return"]


def test_unreadable_presence_is_never_a_break(s):
    row = suggest(s, at(14))
    bad = {"at_pc": False, "idle_s": None, "locked": None, "why": "cannot read presence"}
    assert passes(s, at(14, 5), presence=bad) == [] and passes(s, at(14, 6), presence=AT_PC) == []
    assert outbox.get(row["id"])["state"] == "held"


# ---- the real readers --------------------------------------------------------------------------------------------

def test_the_owners_own_message_is_a_break_but_a_canary_or_test_is_not(s):
    inbound.submit("c", "hello", origin="canary", now=at(14, 5))
    inbound.submit("c", "hello", origin="test", now=at(14, 6))
    assert holdq._owner_messages(at(14)) == []
    inbound.submit("c", "what's new?", origin="user", now=at(14, 7))
    got = holdq._owner_messages(at(14))
    assert [b["type"] for b in got] == ["owner_message"] and got[0]["ts"] == at(14, 7)
    assert holdq._owner_messages(at(14, 8)) == []


def test_push_times_read_update_by_push_lines_only(tmp_path):
    repo = tmp_path / "repo"
    log = repo / ".git" / "logs" / "refs" / "remotes" / "origin"
    log.mkdir(parents=True)
    (log / "main").write_text(
        "0000 aaaa Me <m@x> 1000 +0000\tfetch origin: fast-forward\n"
        "aaaa bbbb Me <m@x> 2000 +0000\tupdate by push\n", encoding="utf-8")
    assert holdq.push_times([str(repo)], since=0) == [(str(repo), 2000.0)]
    assert holdq.push_times([str(repo)], since=2500) == []


def test_commits_and_pushes_become_breaks(s, monkeypatch, tmp_path):
    from waggle import activity
    monkeypatch.setattr(holdq, "_repos", lambda st, now: {str(tmp_path / "r"): "r"})
    monkeypatch.setattr(activity, "commit_times", lambda repos, now=None: {"r": [at(14, 5), at(13)]})
    monkeypatch.setattr(holdq, "push_times", lambda repos, since: [(str(tmp_path / "r"), at(14, 8))])
    got = holdq._repo_breaks(s, at(14), at(14, 10))
    assert sorted((b["type"], b["ts"]) for b in got) == [("commit", at(14, 5)), ("push", at(14, 8))]


def test_a_quiet_claude_code_session_is_a_break_but_waggles_own_runs_are_not(s, monkeypatch):
    from waggle import activity
    sessions = [{"session": "mine", "last": at(14, 0), "machine": False},
                {"session": "waggle-job", "last": at(14, 0), "machine": True},
                {"session": "still-going", "last": at(14, 9), "machine": False}]
    monkeypatch.setattr(activity, "scan_sessions", lambda st, now=None: sessions)
    got = holdq._session_breaks(s, at(13, 50), at(14, 10))
    assert [b["id"] for b in got] == [f"session:mine:{int(at(14, 0))}"] and got[0]["type"] == "session_end"
    assert got[0]["ts"] == at(14, 0) + holdq.SESSION_QUIET_S


def test_unreadable_sessions_and_repos_give_no_breaks_instead_of_errors(s, monkeypatch):
    from waggle import activity
    monkeypatch.setattr(activity, "scan_sessions", lambda st, now=None: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(activity, "commit_times", lambda repos, now=None: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(holdq, "_repos", lambda st, now: {})
    assert holdq._session_breaks(s, 0, at(14)) == [] and holdq._repo_breaks(s, 0, at(14)) == []


# ---- the sender loop -----------------------------------------------------------------------------------------------

def test_the_sender_loop_releases_a_row_past_its_max_wait_and_sends_it(s, monkeypatch):
    monkeypatch.setattr(sender, "_transports", {})
    monkeypatch.setattr(sender, "_before_send", [])
    sent = []
    sender.register("text", lambda row: sent.append(row["body"]) or "m1")
    sender.before_send(holdq.release_hook)
    sender.before_send(holdq.release_hook)                       # registered once
    assert len(sender._before_send) == 1
    row = suggest(s, at(14), text="tests are red")
    assert sender.send_one(now=at(14, 30)) is None and sent == []                 # still held, nothing to do
    assert sender.send_one(now=at(14) + 91 * 60) == "sent" and sent == ["tests are red"]
    r = outbox.get(row["id"])
    assert r["state"] == "sent" and r["meta"]["hold"]["released_by"] == "max_wait"


def test_a_failing_hook_never_stops_sending(s, monkeypatch):
    monkeypatch.setattr(sender, "_transports", {})
    monkeypatch.setattr(sender, "_before_send", [])
    sent = []
    sender.register("text", lambda row: sent.append(row["body"]) or "m1")
    sender.before_send(lambda now: 1 / 0)
    oid = outbox.enqueue("text", "reminder", origin="reminder", now=at(14))
    assert sender.send_one(now=at(14)) == "sent" and sent == ["reminder"] and outbox.get(oid)["state"] == "sent"


def test_the_hook_costs_nothing_when_nothing_is_held(s):
    assert holdq.release_hook(at(14)) == []


def test_status_lists_what_waits(s, capsys):
    suggest(s, at(14), text="look at this")
    holdq.main([])
    out = capsys.readouterr().out
    assert "held now: 1" in out and "look at this" in out
