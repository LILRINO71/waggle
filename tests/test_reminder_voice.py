"""A reminder asked for by voice comes back as a voice note first, then the text line. Speech here is a fake: no
real Kokoro, no real message."""
import datetime as dt
import time

import pytest

from waggle import followups, notify, tick, traces
from waggle.core import jobs, outbox, timers


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def rows():
    return sorted(outbox.list(limit=1000), key=lambda r: r["id"])


def run_reminders(now):
    out = []
    while (j := jobs.run_one(kinds=["outreach.reminder"], now=now)) is not None:
        out.append(j)
    return out


@pytest.fixture
def speech(monkeypatch):
    """A synthesis that succeeds and records what it was asked to say."""
    said = []

    def synth(text, out, settings=None):
        said.append(text)
        out.write_bytes(b"OggS")
        return True
    monkeypatch.setattr(notify, "_synth", synth)
    return said


def turn(minutes_ago, *, voice, origin="user", chat="c1", now=None):
    now = now or time.time()
    return {"time": now - minutes_ago * 60, "origin": origin, "chat": chat, "platform": "telegram",
            "extra": {"voice": voice, "voice_turn": {"voice": True} if voice else None}}


def make(voice, text="stand up", due=at(7, day=20)):
    tick.register_jobs()
    fid = followups.add(due, text, now=at(12), voice=voice)
    return fid


# ---- the decision --------------------------------------------------------------------------------------

def test_a_recent_voice_turn_marks_the_reminder_voice():
    assert followups.asked_by_voice(time.time(), "c1", turns=[turn(2, voice=True)]) is True


def test_a_typed_turn_does_not():
    assert followups.asked_by_voice(time.time(), "c1", turns=[turn(2, voice=False)]) is False


def test_only_the_most_recent_own_turn_counts():
    assert followups.asked_by_voice(time.time(), "c1", turns=[turn(1, voice=False), turn(3, voice=True)]) is False
    assert followups.asked_by_voice(time.time(), "c1", turns=[turn(1, voice=True), turn(3, voice=False)]) is True


def test_the_ten_minute_window():
    now = time.time()
    assert followups.asked_by_voice(now, "c1", turns=[turn(9.5, voice=True, now=now)]) is True
    assert followups.asked_by_voice(now, "c1", turns=[turn(10.5, voice=True, now=now)]) is False


@pytest.mark.parametrize("origin", ["test", "canary", "bench", "agent"])
def test_non_user_turns_never_count(origin):
    assert followups.asked_by_voice(time.time(), "c1", turns=[turn(1, voice=True, origin=origin)]) is False
    # and they do not hide the user's own earlier voice turn either: only user turns are looked at
    assert followups.asked_by_voice(time.time(), "c1",
                                    turns=[turn(1, voice=False, origin=origin), turn(2, voice=True)]) is True


def test_read_from_the_real_trace_store():
    traces.record("remind me to stand up", platform="telegram", chat="c1", origin="user",
                  extra={"voice": True, "voice_turn": {"voice": True}})
    assert followups.asked_by_voice(time.time(), "c1") is True
    traces.record("and one more", platform="telegram", chat="c1", origin="user", extra={"voice": False})
    assert followups.asked_by_voice(time.time(), "c1") is False


def test_add_stores_the_flag_in_the_timer_payload():
    fid = make(True)
    assert timers.get(f"reminder:{fid}")["payload"].get("voice") is True
    fid2 = make(False, text="other")
    assert "voice" not in timers.get(f"reminder:{fid2}")["payload"]


def test_a_bench_reminder_is_never_voice():
    fid = followups.add(at(13), "x", origin="bench", now=at(12), voice=True)
    assert "voice" not in timers.get(f"reminder:{fid}")["payload"]


# ---- the firing ----------------------------------------------------------------------------------------

def test_voice_asked_goes_out_as_voice_then_text(speech):
    fid = make(True)
    timers.fire_due(at(7, day=20))
    run_reminders(at(7, day=20))
    r = rows()
    assert [x["kind"] for x in r] == ["voice", "text"]
    assert speech == ["Sir, your reminder: stand up."]
    assert r[0]["file"] and r[1]["body"] == "⏰ stand up"
    assert followups.get(fid)["status"] == "sent"


def test_typed_goes_out_as_text_only(speech):
    make(False)
    timers.fire_due(at(7, day=20))
    run_reminders(at(7, day=20))
    assert [x["kind"] for x in rows()] == ["text"] and speech == []


def test_speech_failure_sends_the_text_alone_and_records_why(monkeypatch):
    monkeypatch.setattr(notify, "_synth", lambda text, out, settings=None: False)
    fid = make(True)
    timers.fire_due(at(7, day=20))
    done = run_reminders(at(7, day=20))
    assert [x["kind"] for x in rows()] == ["text"]
    assert "speech failed" in followups.get(fid)["reason"]
    assert done[0]["result"]["voice_error"] == "speech failed"


def test_speech_that_raises_is_a_failure_not_a_crash(monkeypatch):
    def boom(text, out, settings=None):
        raise OSError("no audio device")
    monkeypatch.setattr(notify, "_synth", boom)
    fid = make(True)
    timers.fire_due(at(7, day=20))
    run_reminders(at(7, day=20))
    assert [x["kind"] for x in rows()] == ["text"] and "no audio device" in followups.get(fid)["reason"]


def test_a_late_voice_reminder_says_so_in_both_forms(speech):
    fid = make(True)
    timers.catch_up(at(9, 30, day=20))
    run_reminders(at(9, 30, day=20))
    r = rows()
    assert [x["kind"] for x in r] == ["voice", "text"]
    assert speech == ["Sir, your reminder, which is late: stand up."]
    assert "late: the PC was off at 7:00 AM" in r[1]["body"]
    assert "late" in followups.get(fid)["reason"]


def test_checkin_payload_carries_the_flag():
    item = followups._add_checkin("test the shooter", at(20), "i'll test it", at(12), None, at(12), voice=True)
    assert timers.get(f"checkin:{item['id']}")["payload"].get("voice") is True
