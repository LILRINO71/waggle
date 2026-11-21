"""Expected-value outreach (waggle/outreach_ev.py): per-kind welcome odds learned from what the owner did after each
unprompted message, the threshold on both sides, the guard, the evening line and the stats. Fakes only."""
import datetime as dt

import pytest

from waggle import brief, initiative, notify, outreach_ev
from waggle.config import Settings
from waggle.core import outbox, store

DAY = 86400.0


def at(h, m=0, day=20):
    return dt.datetime(2026, 11, day, h, m).timestamp()


NOW = at(15)


@pytest.fixture
def s(settings):
    settings.outreach_ev_enabled = True
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.daily_text_cap = 8
    settings.save()
    return Settings.load()


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.close_all()


def seed(kind, welcome=0, ignored=0, unwelcome=0, sent_at=None, start=1000):
    """Labelled outcomes as the labelling pass would have left them."""
    con = outreach_ev._db()
    oid = start
    for label, n in (("welcome", welcome), ("ignored", ignored), ("unwelcome", unwelcome)):
        for _ in range(n):
            oid += 1
            con.execute("INSERT INTO outcomes VALUES (?,?,?,?,?)", (oid, kind, label, sent_at or NOW - DAY, NOW))
    con.commit()
    con.close()


def sig(kind, key=None, urgent=False, asked=False):
    return {"key": key or f"{kind}:1", "kind": kind, "title": f"{kind} thing", "topic": key or f"{kind}:1",
            "urgent": urgent, "asked": asked}


STATE = {"cap_used": 0, "topics": {}, "automations": 0}


# ---- the odds -----------------------------------------------------------------------------------------------

def test_a_kind_with_no_history_is_always_sent_so_it_can_be_tried(s):
    assert outreach_ev.allow(sig("routine"), s, NOW, dict(STATE)) == (True, "")
    seed("routine", ignored=2)                       # still fewer than MIN_LABELS outcomes
    assert outreach_ev.allow(sig("routine"), s, NOW, dict(STATE)) == (True, "")


def test_the_beta_counts_weigh_welcome_ignored_and_stop_differently(s):
    seed("mail", welcome=2, ignored=4, unwelcome=1, sent_at=NOW)
    o = outreach_ev.odds("mail", NOW)
    assert (o["welcome"], o["ignored"], o["unwelcome"], o["n"]) == (2, 4, 1, 7)
    assert o["a"] == pytest.approx(1 + 2) and o["b"] == pytest.approx(1 + 4 * 0.5 + 1 * 3.0)
    assert o["p"] == pytest.approx(3 / (3 + 6), abs=1e-3)


def test_a_stop_costs_more_than_an_ignore(s):
    seed("a_kind", ignored=1, start=10)
    seed("b_kind", unwelcome=1, start=20)
    assert outreach_ev.odds("b_kind", NOW)["p"] < outreach_ev.odds("a_kind", NOW)["p"]


def test_old_outcomes_count_half_as_much_every_30_days(s):
    seed("mail", ignored=4, sent_at=NOW - 30 * DAY)
    assert outreach_ev.odds("mail", NOW)["b"] == pytest.approx(1 + 4 * 0.5 * 0.5)


def test_odds_are_per_kind(s):
    seed("routine", ignored=6, start=10)
    seed("followup", welcome=6, start=20)
    assert outreach_ev.odds("routine", NOW)["p"] < 0.25 < 0.75 < outreach_ev.odds("followup", NOW)["p"]
    assert outreach_ev.odds("never_seen", NOW)["p"] == 0.5


# ---- the decision: both sides of the threshold ----------------------------------------------------------------------

def test_a_kind_the_owner_usually_ignores_is_held_back_and_says_why(s):
    seed("routine", ignored=8)
    ok, note = outreach_ev.allow(sig("routine"), s, NOW, dict(STATE))
    assert ok is False and note.startswith(outreach_ev.NOTE) and "evening note" in note


def test_a_welcome_kind_is_sent(s):
    seed("followup", welcome=5, ignored=1)
    assert outreach_ev.allow(sig("followup"), s, NOW, dict(STATE)) == (True, "")


def test_a_valuable_kind_survives_a_few_ignores_a_cheap_one_does_not(s):
    seed("followup", ignored=3, start=10)            # p = 1 / (1 + 1 + 1.5) = 0.29 x value 1.0 > cost 0.25
    seed("routine", ignored=3, start=20)             # p = 0.29 x value 0.4 < cost 0.25
    assert outreach_ev.allow(sig("followup"), s, NOW, dict(STATE))[0] is True
    assert outreach_ev.allow(sig("routine"), s, NOW, dict(STATE))[0] is False


def test_the_threshold_moves_the_line_both_ways(s):
    seed("mail", welcome=3, ignored=3)               # p = 4 / 6.5 = 0.62 x 0.7 = 0.43 - 0.25 = 0.18
    assert outreach_ev.allow(sig("mail"), s, NOW, dict(STATE))[0] is True
    s.outreach_ev_threshold = 0.3
    assert outreach_ev.allow(sig("mail"), s, NOW, dict(STATE))[0] is False
    s.outreach_ev_threshold = -1.0
    seed("routine", ignored=20, start=500)
    assert outreach_ev.allow(sig("routine"), s, NOW, dict(STATE))[0] is True


def test_the_cost_of_interrupting_rises_with_how_much_was_already_sent_today(s):
    seed("mail", welcome=2, ignored=4)               # p = 3 / 6 = 0.5 x 0.7 = 0.35
    assert outreach_ev.allow(sig("mail"), s, NOW, {"cap_used": 0})[0] is True       # cost 0.25
    assert outreach_ev.allow(sig("mail"), s, NOW, {"cap_used": 6})[0] is False      # cost 0.4375


def test_the_switch_off_sends_everything(s):
    seed("routine", ignored=20)
    s.outreach_ev_enabled = False
    assert outreach_ev.allow(sig("routine"), s, NOW, dict(STATE)) == (True, "")


# ---- the guard -----------------------------------------------------------------------------------------------------

def test_guard_turns_an_ignored_kind_into_an_evening_item(s):
    seed("routine", ignored=8)
    state = {"topics": {}, "cap_used": 0, "automations": 0}
    action, note = initiative.guard(sig("routine"), "text", s, NOW, state)
    assert action == "evening" and note.startswith(outreach_ev.NOTE)
    assert initiative.guard(sig("routine"), "voice", s, NOW, state)[0] == "evening"
    assert initiative.guard(sig("followup"), "text", s, NOW, state)[0] == "text"        # another kind, unaffected


def test_urgent_and_asked_items_bypass_the_odds(s):
    seed("routine", ignored=20)
    state = {"topics": {}, "cap_used": 0, "automations": 0}
    assert initiative.guard(sig("routine", urgent=True), "text", s, NOW, state)[0] == "text"
    assert initiative.guard(sig("routine", asked=True, key="r2"), "text", s, NOW, state)[0] == "text"


def test_a_bug_in_the_odds_never_stops_a_message(s, monkeypatch):
    monkeypatch.setattr(outreach_ev, "allow", lambda *a, **k: 1 / 0)
    state = {"topics": {}, "cap_used": 0, "automations": 0}
    assert initiative.guard(sig("routine"), "text", s, NOW, state)[0] == "text"


# ---- labelling what the owner did --------------------------------------------------------------------------------

def sent_decision(kind, sent_at, text="msg", urgent=0, asked=False, now=NOW, status="sent"):
    """A decided message that went out at sent_at (decision row + a sent outbox row)."""
    row = notify.deliver("text", text, origin="initiative", topic=text, decision_id=text, urgent=bool(urgent),
                         asked=asked, now=sent_at, settings=Settings.load())
    if status == "sent":
        outbox.claim_due(now=sent_at)
        outbox.mark_sent(row["id"], "m-" + text, now=sent_at)
    con = initiative._db()
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"d-{text}", sent_at, text, kind, text, "text", "text", text, "", "", "x", row["id"], urgent))
    con.commit()
    con.close()
    return row["id"]


def labels():
    con = outreach_ev._db()
    out = {r[0]: r[1] for r in con.execute("SELECT outbox_id, label FROM outcomes")}
    con.close()
    return out


def test_a_reply_within_the_window_is_welcome_silence_after_it_is_ignored(s):
    a = sent_decision("github_ci", at(9), "a")
    b = sent_decision("mail", at(9, 5), "b")
    c = sent_decision("routine", at(14), "c")        # window (6 h) still open at 15:00
    turns = [(at(9, 30), "telegram", "thanks, fixing it")]
    # the first message got the reply; at 15:00 the 09:05 message's window has closed with no later message... but the
    # one reply at 09:30 falls inside both windows, so both are welcome; only a message nobody answered is ignored
    outreach_ev.label_outcomes(s, at(16), turns=turns)
    assert labels() == {a: "welcome", b: "welcome"}                  # c: 14:00 + 6 h is still ahead of 16:00
    outreach_ev.label_outcomes(s, at(21), turns=turns)
    assert labels()[c] == "ignored"


def test_stop_and_not_now_are_strongly_unwelcome(s):
    a = sent_decision("routine", at(9), "a")
    b = sent_decision("github_ci", at(10), "b")
    outreach_ev.label_outcomes(s, at(18), turns=[(at(9, 10), "telegram", "ok not now"),
                                                 (at(10, 20), "telegram", "ok, on it")])
    assert labels() == {a: "unwelcome", b: "welcome"}


def test_a_long_message_that_contains_stop_is_not_a_complaint(s):
    assert outreach_ev.is_stop("stop sending these")
    assert outreach_ev.is_stop("Not now.")
    assert outreach_ev.is_stop("too many messages today")
    long_text = "can you stop the shooter loop from spinning after the first ball, it keeps going and " * 3
    assert not outreach_ev.is_stop(long_text)
    assert not outreach_ev.is_stop("") and not outreach_ev.is_stop("thanks!")


def test_only_unprompted_messages_the_owner_could_have_ignored_are_labelled(s):
    urgent = sent_decision("github_ci", at(9), "urgent", urgent=1)
    asked = sent_decision("followup", at(9, 5), "asked", asked=True)
    unsent = sent_decision("mail", at(9, 10), "unsent", status="queued")
    plain = sent_decision("routine", at(9, 15), "plain")
    outreach_ev.label_outcomes(s, at(18), turns=[])
    assert labels() == {plain: "ignored"} and {urgent, asked, unsent}.isdisjoint(labels())


def test_other_platforms_and_other_people_do_not_count_as_a_reply(s):
    a = sent_decision("routine", at(9), "a")
    outreach_ev.label_outcomes(s, at(18), turns=[(at(9, 30), "cli", "anything")])
    assert labels() == {a: "ignored"}


def test_labelling_is_done_once_and_by_track_responses(s):
    a = sent_decision("github_ci", at(9), "a")
    assert initiative.track_responses(s, at(10), turns=[(at(9, 30), "telegram", "got it")]) == 1
    assert labels() == {a: "welcome"}
    assert outreach_ev.label_outcomes(s, at(20), turns=[]) == 0       # already labelled: not redone as ignored
    assert labels() == {a: "welcome"}


def test_labelling_learns_even_when_the_filter_is_switched_off(s):
    s.outreach_ev_enabled = False
    a = sent_decision("routine", at(9), "a")
    outreach_ev.label_outcomes(s, at(18), turns=[])
    assert labels() == {a: "ignored"}


# ---- the evening line and the stats ---------------------------------------------------------------------------------

def test_the_evening_note_says_how_many_suggestions_were_held_back(s):
    seed("routine", ignored=8)
    initiative.record([{"key": "r1", "kind": "routine", "title": "r1 happened", "detail": "", "topic": "r1",
                        "urgent": False}, {"key": "r2", "kind": "routine", "title": "r2 happened", "detail": "",
                                           "topic": "r2", "urgent": False}], NOW)
    ask = lambda prompt, **k: {"json": {"decisions": [{"key": x, "action": "text", "message": f"msg {x}",
                                                       "reason": "model wants it"} for x in ("r1", "r2")]}}
    out = initiative.decide(s, NOW, asker=ask, gate=False)
    assert [d["action"] for d in out] == ["evening", "evening"]
    assert all(outreach_ev.NOTE in d["guard"] for d in out)
    assert outreach_ev.evening_line(NOW) == "Held back 2 suggestions (kinds you usually ignore)."
    assert [x["key"] for x in initiative.evening_items(NOW, s)] == ["r1", "r2"]      # nothing lost: tonight's note
    assert "Held back 2 suggestions (kinds you usually ignore)." in brief.evening_note(s, now=at(21))


def test_no_line_when_nothing_was_held_back(s):
    assert outreach_ev.evening_line(NOW) == ""
    assert "Held back" not in brief.evening_note(s, now=at(21))


def test_stats_show_the_odds_per_kind_and_what_would_happen(s, capsys):
    seed("routine", ignored=8, start=10)
    seed("followup", welcome=4, ignored=1, start=20)
    seed("mail", welcome=1, start=30)
    assert initiative.main(["stats"]) == 0
    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines() if line.split() and line.split()[0] in
            ("routine", "followup", "mail")}
    assert "held for the evening note" in rows["routine"] and rows["followup"].endswith("sent")
    assert "too few outcomes" in rows["mail"]
    assert "false-alarm rate" in out and "enabled=True" in out
    assert initiative.main(["bogus"]) == 2
