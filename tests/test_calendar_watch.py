"""Calendar watch: the owner's Google Calendar (secret iCal address) with no Claude usage.

Nothing real is reached: a fake fetcher returns invented ICS text, the conftest's in-memory Credential Manager holds
an invented secret address, urllib is blocked, and the test's own runtime.db outbox shows what would be texted. The
PC's zone is pinned to America/Los_Angeles (the built-in US rules) so the results do not depend on the machine."""
import datetime as dt
import json
import logging
import urllib.error
import urllib.request

import pytest

from waggle import brief, calendar_watch as cw, wincred
from waggle.config import data_path
from waggle.core import jobs, outbox, plugins, store, timers

SECRET = "https://calendar.google.com/calendar/ical/owner%40example.com/private-0123456789abcdef0123456789abcdef/basic.ics"
LA = cw.UsTz(-8, "PT")


def T(m, d, h=0, mi=0, y=2026):
    """Epoch of a Los Angeles wall-clock time."""
    return dt.datetime(y, m, d, h, mi, tzinfo=LA).timestamp()


def local(ts):
    return dt.datetime.fromtimestamp(ts, LA).strftime("%m-%d %H:%M")


VTZ_LA = ["BEGIN:VTIMEZONE", "TZID:America/Los_Angeles", "BEGIN:DAYLIGHT", "TZOFFSETFROM:-0800", "TZOFFSETTO:-0700",
          "TZNAME:PDT", "DTSTART:19700308T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "END:DAYLIGHT",
          "BEGIN:STANDARD", "TZOFFSETFROM:-0700", "TZOFFSETTO:-0800", "TZNAME:PST", "DTSTART:19701101T020000",
          "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "END:STANDARD", "END:VTIMEZONE"]


def ev(*lines):
    return ["BEGIN:VEVENT", *lines, "END:VEVENT"]


def ics(*events, extra=()):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN", "X-WR-TIMEZONE:America/Los_Angeles", *VTZ_LA,
             *extra]
    for e in events:
        lines += e
    return "\r\n".join(lines + ["END:VCALENDAR", ""])


def occ(text, start, end):
    """Occurrences between two (month, day) pairs of 2026 (the end may be given as (month, day, year))."""
    events, ctx = cw.parse_ics(text, LA)
    y2 = end[2] if len(end) > 2 else 2026
    return cw.occurrences(events, T(*start), T(end[0], end[1], y=y2), ctx)


def shown(os_):
    return [local(o["start"]) if not o["all_day"] else o["start_day"][5:] + " all-day" for o in os_]


ROBOTICS = ev("DTSTART;TZID=America/Los_Angeles:20261119T160000", "DTEND;TZID=America/Los_Angeles:20261119T173000",
              "UID:robo1@google.com", "SUMMARY:Robotics meeting", "LOCATION:Room 204", "STATUS:CONFIRMED")


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(cw, "local_tz", lambda: LA)

    def no_net(*a, **k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(cw, "FETCH", no_net)
    monkeypatch.setattr(urllib.request, "urlopen", no_net)


class Feed:
    """A fake fetcher: serves `body`, honours ETag, can fail."""

    def __init__(self, body, etag='"v1"'):
        self.body, self.etag, self.fail, self.calls = body, etag, None, []

    def __call__(self, url, etag="", lm=""):
        self.calls.append((url, etag, lm))
        if self.fail:
            raise cw.CalError(self.fail)
        if etag and etag == self.etag:
            return 304, None, etag, ""
        return 200, self.body, self.etag, "Wed, 18 Nov 2026 10:00:00 GMT"


def set_up(monkeypatch, body, name="personal", url=SECRET):
    wincred.write(cw.CRED_PREFIX + name, name, url)
    names = [c["name"] for c in (cw._config("calendars", []) or [])]
    cw._set_config("calendars", [{"name": n} for n in dict.fromkeys([*names, name])])
    feed = Feed(body)
    monkeypatch.setattr(cw, "FETCH", feed)
    return feed


def texts():
    return [r["body"] for r in sorted(outbox.list(limit=1000), key=lambda r: r["id"])]


# ---- time zones ----------------------------------------------------------------------------------------------

def test_the_builtin_us_zone_follows_the_dst_rules():
    assert LA.utcoffset(dt.datetime(2026, 11, 1, 0, 59)) == dt.timedelta(hours=-7)
    assert LA.utcoffset(dt.datetime(2026, 11, 2, 12)) == dt.timedelta(hours=-8)
    assert LA.utcoffset(dt.datetime(2026, 3, 8, 1, 59)) == dt.timedelta(hours=-8)
    assert LA.utcoffset(dt.datetime(2026, 3, 8, 3, 0)) == dt.timedelta(hours=-7)
    assert dt.datetime(2026, 7, 1, 19, tzinfo=dt.timezone.utc).astimezone(LA).hour == 12  # PDT
    assert dt.datetime(2026, 12, 1, 19, tzinfo=dt.timezone.utc).astimezone(LA).hour == 11  # PST
    assert dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.timezone.utc).astimezone(LA).hour == 1  # first 1 o'clock (PDT)
    assert dt.datetime(2026, 11, 1, 9, 30, tzinfo=dt.timezone.utc).astimezone(LA).fold == 1  # the second one


def test_the_pcs_own_zone_class_converts_both_ways_without_a_tz_database():
    z = cw.LocalTz()
    d = dt.datetime(2026, 7, 1, 12, tzinfo=z)
    back = dt.datetime.fromtimestamp(d.timestamp(), z)
    assert (back.year, back.month, back.day, back.hour) == (2026, 7, 1, 12)


def test_a_tzid_is_read_with_zoneinfo_or_the_calendars_vtimezone_or_the_builtin_table_then_the_pc():
    custom = ["BEGIN:VTIMEZONE", "TZID:Custom/Plus3", "BEGIN:STANDARD", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300",
              "DTSTART:19700101T000000", "END:STANDARD", "END:VTIMEZONE"]
    text = ics(ev("DTSTART;TZID=America/Los_Angeles:20260701T160000", "UID:a", "SUMMARY:Summer"),
               ev("DTSTART;TZID=Custom/Plus3:20261119T120000", "UID:b", "SUMMARY:Custom"),
               ev("DTSTART;TZID=Mars/Olympus:20261119T120000", "UID:c", "SUMMARY:Unknown"),
               ev("DTSTART;TZID=America/Chicago:20261119T120000", "UID:d", "SUMMARY:Chicago"),
               extra=custom)
    events, ctx = cw.parse_ics(text, LA)
    got = {e["uid"]: e["start"].timestamp() for e in events}
    utc = dt.timezone.utc
    assert got["a"] == dt.datetime(2026, 7, 1, 23, tzinfo=utc).timestamp()  # PDT, UTC-7
    assert got["b"] == dt.datetime(2026, 11, 19, 9, tzinfo=utc).timestamp()  # +03:00 from the calendar's VTIMEZONE
    assert got["c"] == T(11, 19, 12)  # unknown zone: the PC's local time
    assert got["d"] == dt.datetime(2026, 11, 19, 18, tzinfo=utc).timestamp()  # built-in table (CST, UTC-6)
    assert any("Mars/Olympus" in n for n in ctx.notes)  # and it says so


# ---- parsing -------------------------------------------------------------------------------------------------

def test_timed_utc_tzid_and_all_day_events_parse_with_their_text():
    text = ics(ROBOTICS,
               ev("DTSTART:20261120T000000Z", "DTEND:20261120T010000Z", "UID:utc1", "SUMMARY:Call\\, with Sam",
                  "LOCATION:Zoom"),
               ev("DTSTART;VALUE=DATE:20261123", "DTEND;VALUE=DATE:20261128", "UID:break", "SUMMARY:Thanksgiving break"),
               ev("DTSTART;TZID=America/Los_Angeles:20261119T180000", "UID:fold", "SUMMARY:A very long", " title that",
                  "  folds"))
    got = {o["uid"]: o for o in occ(text, (11, 19), (12, 5))}
    r = got["robo1@google.com"]
    assert local(r["start"]) == "11-19 16:00" and local(r["end"]) == "11-19 17:30" and r["location"] == "Room 204"
    assert local(got["utc1"]["start"]) == "11-19 16:00" and got["utc1"]["summary"] == "Call, with Sam"
    b = got["break"]
    assert b["all_day"] and (b["start_day"], b["end_day"]) == ("2026-11-23", "2026-11-28")
    assert got["fold"]["summary"] == "A very longtitle that folds"  # a fold drops one leading space
    assert got["fold"]["end"] == got["fold"]["start"]  # no end: a point in time


def test_duration_instead_of_an_end():
    text = ics(ev("DTSTART;TZID=America/Los_Angeles:20261119T160000", "DURATION:PT1H30M", "UID:d", "SUMMARY:Lab"))
    (o,) = occ(text, (11, 19), (11, 20))
    assert local(o["end"]) == "11-19 17:30"


def test_garbage_and_non_calendars_are_refused_and_bad_events_skipped():
    with pytest.raises(cw.CalError) as e:
        cw.parse_ics("<html>sign in</html>", LA)
    assert e.value.kind == "not a calendar"
    text = ics(ev("DTSTART:notadate", "UID:bad", "SUMMARY:Bad"), ROBOTICS)
    assert [o["uid"] for o in occ(text, (11, 19), (11, 20))] == ["robo1@google.com"]


# ---- recurrence ----------------------------------------------------------------------------------------------

def rec(start, rule, *more, tz="America/Los_Angeles", uid="r"):
    end = f"{start[:9]}{int(start[9:11]) + 1:02d}{start[11:]}"
    return ev(f"DTSTART;TZID={tz}:{start}", f"DTEND;TZID={tz}:{end}", f"UID:{uid}", "SUMMARY:Rec", f"RRULE:{rule}",
              *more)


def test_daily_with_count_and_exdate():
    text = ics(rec("20261119T090000", "FREQ=DAILY;COUNT=4", "EXDATE;TZID=America/Los_Angeles:20261121T090000"))
    assert shown(occ(text, (11, 1), (12, 31))) == ["11-19 09:00", "11-20 09:00", "11-22 09:00"]  # COUNT counts the exdate


def test_weekly_with_byday_interval_and_until():
    text = ics(rec("20261116T180000", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20261217T075959Z"))
    assert shown(occ(text, (11, 1), (12, 31))) == ["11-16 18:00", "11-18 18:00", "11-30 18:00", "12-02 18:00",
                                                   "12-14 18:00", "12-16 18:00"]


def test_a_weekly_event_keeps_its_wall_clock_time_across_the_november_dst_change():
    text = ics(rec("20261026T160000", "FREQ=WEEKLY;COUNT=3"))
    a, b, c = occ(text, (10, 20), (11, 30))
    assert [local(x["start"]) for x in (a, b, c)] == ["10-26 16:00", "11-02 16:00", "11-09 16:00"]
    assert (b["start"] - a["start"]) == 7 * 86400 + 3600  # an hour longer in UTC terms: the clocks went back


def test_monthly_by_month_day_nth_weekday_last_weekday_and_missing_days():
    to = (3, 1, 2027)
    assert shown(occ(ics(rec("20261115T100000", "FREQ=MONTHLY;COUNT=3")), (11, 1), to)) == [
        "11-15 10:00", "12-15 10:00", "01-15 10:00"]
    assert shown(occ(ics(rec("20261110T100000", "FREQ=MONTHLY;BYDAY=2TU;COUNT=3")), (11, 1), to)) == [
        "11-10 10:00", "12-08 10:00", "01-12 10:00"]
    assert shown(occ(ics(rec("20261127T100000", "FREQ=MONTHLY;BYDAY=-1FR;COUNT=3")), (11, 1), to)) == [
        "11-27 10:00", "12-25 10:00", "01-29 10:00"]
    assert shown(occ(ics(rec("20261031T100000", "FREQ=MONTHLY;COUNT=3")), (10, 1), to)) == [
        "10-31 10:00", "12-31 10:00", "01-31 10:00"]  # no 31st in November
    assert shown(occ(ics(rec("20261115T100000", "FREQ=MONTHLY;BYMONTHDAY=1,-1;COUNT=4")), (11, 1), to)) == [
        "11-30 10:00", "12-01 10:00", "12-31 10:00", "01-01 10:00"]


def test_yearly_and_an_unsupported_rule_shows_the_first_event_and_says_so():
    got = shown(occ(ics(rec("20261201T090000", "FREQ=YEARLY")), (11, 1), (12, 31, 2028)))
    assert got == ["12-01 09:00", "12-01 09:00", "12-01 09:00"]
    events, ctx = cw.parse_ics(ics(rec("20261119T090000", "FREQ=HOURLY;COUNT=5")), LA)
    one = cw.occurrences(events, T(11, 19), T(11, 21), ctx)
    assert len(one) == 1 and any("not supported" in n for n in ctx.notes)


def test_all_day_recurrence_and_until_date():
    text = ics(ev("DTSTART;VALUE=DATE:20261120", "DTEND;VALUE=DATE:20261121", "UID:ad", "SUMMARY:Trash day",
                  "RRULE:FREQ=WEEKLY;UNTIL=20261211"))
    assert shown(occ(text, (11, 1), (12, 31))) == ["11-20 all-day", "11-27 all-day", "12-04 all-day", "12-11 all-day"]


def test_an_override_moves_one_occurrence_and_a_cancelled_one_disappears():
    master = rec("20261119T160000", "FREQ=WEEKLY;COUNT=4", uid="w")
    moved = ev("RECURRENCE-ID;TZID=America/Los_Angeles:20261126T160000",
               "DTSTART;TZID=America/Los_Angeles:20261126T180000", "DTEND;TZID=America/Los_Angeles:20261126T190000",
               "UID:w", "SUMMARY:Rec (moved)")
    gone = ev("RECURRENCE-ID;TZID=America/Los_Angeles:20261203T160000",
              "DTSTART;TZID=America/Los_Angeles:20261203T160000", "UID:w", "SUMMARY:Rec", "STATUS:CANCELLED")
    got = occ(ics(master, moved, gone), (11, 1), (12, 31))
    assert [(local(o["start"]), o["summary"]) for o in got] == [("11-19 16:00", "Rec"), ("11-26 18:00", "Rec (moved)"),
                                                                 ("12-10 16:00", "Rec")]


def test_a_cancelled_event_and_a_cancelled_series_are_dropped():
    text = ics(ev("DTSTART;TZID=America/Los_Angeles:20261119T160000", "UID:x", "SUMMARY:Gone", "STATUS:CANCELLED"),
               ev("DTSTART;TZID=America/Los_Angeles:20261119T170000", "UID:y", "SUMMARY:Kept", "STATUS:TENTATIVE"),
               rec("20261119T180000", "FREQ=DAILY;COUNT=3", "STATUS:CANCELLED", uid="z"))
    assert [o["uid"] for o in occ(text, (11, 19), (11, 30))] == ["y"]


def test_a_rule_that_never_matches_stops():
    text = ics(rec("20000101T090000", "FREQ=DAILY;BYMONTHDAY=31;BYMONTH=2"))
    assert occ(text, (11, 19), (11, 20)) == []


# ---- text ----------------------------------------------------------------------------------------------------

def test_headsup_and_brief_wording():
    o = {"summary": "Robotics meeting", "location": "Room 204", "start": T(11, 19, 16), "end": T(11, 19, 17, 30),
         "all_day": False}
    assert cw.headsup_text(o, T(11, 19, 15, 30), LA) == "In 30 min: Robotics meeting (Room 204), 4:00-5:30 PM."
    assert cw.headsup_text(o, T(11, 19, 14, 30), LA) == "In 1 hr 30 min: Robotics meeting (Room 204), 4:00-5:30 PM."
    o2 = {**o, "start": T(11, 19, 11, 30), "end": T(11, 19, 13), "location": "Room 204, Lincoln High School, 1 Main St"}
    assert cw.headsup_text(o2, T(11, 19, 11), LA) == ("In 30 min: Robotics meeting (Room 204, Lincoln High School), "
                                                      "11:30 AM-1:00 PM.")
    assert cw.headsup_text({**o, "end": o["start"], "location": ""}, T(11, 19, 15, 59), LA) == \
        "In 1 min: Robotics meeting, 4:00 PM."
    assert cw.event_line({**o, "all_day": True}, LA) == "all day: Robotics meeting (Room 204)"


# ---- the store, the fetch duty ------------------------------------------------------------------------------

BODY = ics(ROBOTICS,
           ev("DTSTART;VALUE=DATE:20261123", "DTEND;VALUE=DATE:20261124", "UID:ad", "SUMMARY:Spring concert day"),
           ev("DTSTART;TZID=America/Los_Angeles:20261119T080000", "DTEND;TZID=America/Los_Angeles:20261119T090000",
              "UID:early", "SUMMARY:Dentist"),
           ev("DTSTART;TZID=America/Los_Angeles:20261210T080000", "UID:far", "SUMMARY:Beyond fourteen days"))


def test_a_fetch_stores_the_next_14_days_and_today_and_upcoming_read_them(monkeypatch):
    feed = set_up(monkeypatch, BODY)
    out = cw.run_fetch(now=T(11, 19, 7, 30))
    assert out["errors"] == {} and out["calendars"][0]["events"] == 3  # the Dec 10 event is outside the window
    assert [o["summary"] for o in cw.today(now=T(11, 19, 7, 30))] == ["Dentist", "Robotics meeting"]
    up = cw.upcoming(14, now=T(11, 19, 12))
    assert [o["summary"] for o in up] == ["Robotics meeting", "Spring concert day"]  # the dentist is over
    assert [o["summary"] for o in cw.upcoming(2, now=T(11, 19, 12))] == ["Robotics meeting"]
    assert feed.calls[0][1] == ""  # first time: no ETag yet


def test_an_unchanged_calendar_answers_304_and_the_window_still_rolls_forward(monkeypatch):
    feed = set_up(monkeypatch, BODY)
    cw.run_fetch(now=T(11, 19, 7, 30))
    out = cw.run_fetch(now=T(11, 25, 7, 30))  # six days on, the same ETag
    assert feed.calls[1][1] == '"v1"' and out["calendars"][0]["not_modified"]
    assert cw.upcoming(14, now=T(11, 25, 8)) == []  # the Nov 19 and Nov 23 events are past
    feed.body, feed.etag = ics(ev("DTSTART;TZID=America/Los_Angeles:20261203T080000", "UID:late",
                                  "SUMMARY:Dec 3 thing")), '"v2"'
    cw.run_fetch(now=T(11, 25, 7, 45))
    assert [o["summary"] for o in cw.upcoming(14, now=T(11, 25, 8))] == ["Dec 3 thing"]


def test_a_failed_fetch_keeps_what_was_stored_and_three_in_a_row_send_one_text_a_day(monkeypatch):
    feed = set_up(monkeypatch, BODY)
    cw.run_fetch(now=T(11, 19, 7, 30))
    feed.fail = "http 404"
    for i in range(5):
        out = cw.run_fetch(now=T(11, 19, 8) + 900 * i)
    assert out["errors"] == {"personal": "http 404"}
    assert [o["summary"] for o in cw.today(now=T(11, 19, 9))] == ["Dentist", "Robotics meeting"]  # kept
    told = [t for t in texts() if "can't read your calendar" in t]
    assert len(told) == 1 and "[personal]" in told[0] and "login" in told[0]
    cw.run_fetch(now=T(11, 20, 8))
    assert len([t for t in texts() if "can't read your calendar" in t]) == 2  # a new day: one more
    feed.fail = None
    cw.run_fetch(now=T(11, 20, 8, 15))
    st = cw.status(now=T(11, 20, 8, 20))["calendars"][0]
    assert st["failed_in_a_row"] == 0 and st["last_error"] is None and st["events_kept"] == 1  # Nov 20: only the 23rd is left


def test_the_fetch_waits_for_waking_hours_and_needs_a_calendar(monkeypatch):
    assert cw.run_fetch(now=T(11, 19, 12))["skipped"].startswith("no calendar set up")
    set_up(monkeypatch, BODY)
    assert cw.run_fetch(now=T(11, 19, 3))["skipped"].startswith("outside waking hours")
    assert cw.run_fetch(now=T(11, 19, 3), force=True)["calendars"]


def test_two_calendars_are_read_separately_and_one_failing_does_not_block_the_other(monkeypatch):
    set_up(monkeypatch, BODY, name="personal")
    other = ics(ev("DTSTART;TZID=America/Los_Angeles:20261119T190000", "UID:s1", "SUMMARY:School play"))
    set_up(monkeypatch, other, name="school", url=SECRET.replace("owner", "school"))

    def fetch(url, etag="", lm=""):
        if "school" in url:
            return 200, other, "", ""
        raise cw.CalError("timeout")
    monkeypatch.setattr(cw, "FETCH", fetch)
    assert cw.calendars() == ["personal", "school"]
    out = cw.run_fetch(now=T(11, 19, 7, 30))
    assert out["errors"] == {"personal": "timeout"} and out["calendars"][0]["calendar"] == "school"
    assert [o["summary"] for o in cw.today(now=T(11, 19, 8))] == ["School play"]


def test_dry_run_reads_but_writes_and_sends_nothing(monkeypatch):
    set_up(monkeypatch, BODY)
    out = cw.run_fetch(now=T(11, 19, 15, 40), dry_run=True)
    assert out["calendars"][0]["events"] == 3 and out["errors"] == {}
    assert not data_path(cw.DB_NAME).exists() and texts() == []
    occs = [{**o, "ref": o["key"]} for o in out["calendars"][0]["occurrences"]]
    assert [t for _, t in cw.due_headsups(occs, T(11, 19, 15, 40), 30)] == [
        "In 20 min: Robotics meeting (Room 204), 4:00-5:30 PM."]


# ---- heads-ups -----------------------------------------------------------------------------------------------

def stored(monkeypatch, body=BODY, now=T(11, 19, 7, 30)):
    feed = set_up(monkeypatch, body)
    cw.run_fetch(now=now)
    return feed


def test_a_heads_up_goes_out_once_thirty_minutes_before_a_timed_event(monkeypatch):
    stored(monkeypatch)
    assert cw.send_headsups(now=T(11, 19, 15, 29))["sent"] == 0  # 31 min before
    assert cw.send_headsups(now=T(11, 19, 15, 31))["sent"] == 1
    for minute in (36, 41, 46, 51, 59):  # every later 5-min tick, however many
        assert cw.send_headsups(now=T(11, 19, 15, minute))["sent"] == 0
    assert cw.send_headsups(now=T(11, 19, 16, 1))["sent"] == 0  # after the start: nothing
    assert texts() == ["In 29 min: Robotics meeting (Room 204), 4:00-5:30 PM."]
    row = outbox.list(limit=10)[0]
    assert row["origin"] == "calendar" and row["idem_key"].startswith("calendar:personal:robo1@google.com:")
    assert row["meta"]["asked"] is True


def test_a_lost_table_or_a_repeat_fetch_cannot_make_it_twice(monkeypatch):
    stored(monkeypatch)
    cw.send_headsups(now=T(11, 19, 15, 40))
    con = cw._db()
    con.execute("DELETE FROM headsup")  # even if our own table were lost, the outbox key still holds
    con.close()
    cw.send_headsups(now=T(11, 19, 15, 45))
    cw.fetch_job(now=T(11, 19, 15, 50), fetch=lambda *a: (200, BODY, '"v9"', ""))
    assert len(texts()) == 1


def test_all_day_events_get_no_heads_up_and_a_moved_event_gets_a_new_one(monkeypatch):
    feed = stored(monkeypatch, ics(ev("DTSTART;VALUE=DATE:20261119", "DTEND;VALUE=DATE:20261120", "UID:ad",
                                      "SUMMARY:Spirit day"), ROBOTICS))
    assert cw.send_headsups(now=T(11, 19, 8))["sent"] == 0 and texts() == []  # the all-day event never counts
    cw.send_headsups(now=T(11, 19, 15, 40))
    moved = ROBOTICS[:1] + ["DTSTART;TZID=America/Los_Angeles:20261119T170000",
                            "DTEND;TZID=America/Los_Angeles:20261119T180000"] + ROBOTICS[3:]
    feed.body, feed.etag = ics(moved), '"v2"'
    cw.run_fetch(now=T(11, 19, 15, 45))
    cw.send_headsups(now=T(11, 19, 16, 40))
    assert texts() == ["In 20 min: Robotics meeting (Room 204), 4:00-5:30 PM.",
                       "In 20 min: Robotics meeting (Room 204), 5:00-6:00 PM."]


def test_the_lead_time_is_configurable_and_zero_turns_it_off(monkeypatch):
    stored(monkeypatch)
    cw._set_config("calendar_headsup_min", 10)
    assert cw.send_headsups(now=T(11, 19, 15, 45))["sent"] == 0
    assert cw.send_headsups(now=T(11, 19, 15, 51))["sent"] == 1
    cw._set_config("calendar_headsup_min", 0)
    assert cw.send_headsups(now=T(11, 19, 15, 55)) == {"sent": 0, "disabled": True}


QUIET = ics(ev("DTSTART;TZID=America/Los_Angeles:20261119T071500", "UID:early", "SUMMARY:Practice"),
            ev("DTSTART;TZID=America/Los_Angeles:20261119T233000", "UID:late", "SUMMARY:Late call"),
            ev("DTSTART;TZID=America/Los_Angeles:20261119T070000", "UID:sharp", "SUMMARY:Sharp"))


def test_quiet_hours_an_event_inside_them_is_told_at_once_one_after_them_waits_for_the_true_minutes(monkeypatch,
                                                                                                    settings):
    settings.quiet_hours = [23, 7]
    settings.save()
    stored(monkeypatch, QUIET, now=T(11, 19, 6, 30))
    assert cw.send_headsups(now=T(11, 19, 22, 59))["sent"] == 0
    assert cw.send_headsups(now=T(11, 19, 23, 5))["sent"] == 1  # 23:30 is inside quiet hours: it goes at once
    assert texts()[-1] == "In 25 min: Late call, 11:30 PM."
    assert cw.send_headsups(now=T(11, 19, 6, 35))["sent"] == 1  # 07:00 sharp: quiet ends as it starts, no waiting
    assert texts()[-1] == "In 25 min: Sharp, 7:00 AM."
    assert cw.send_headsups(now=T(11, 19, 6, 45))["sent"] == 0  # 07:15 after quiet hours: 06:45 is inside them
    assert cw.send_headsups(now=T(11, 19, 7, 0))["sent"] == 1  # the first tick after: the true minutes left
    assert texts()[-1] == "In 15 min: Practice, 7:15 AM."
    assert all(r["meta"]["held"] is False for r in outbox.list(limit=10))


# ---- the brief -----------------------------------------------------------------------------------------------

def test_the_brief_reads_the_calendar_store_and_never_asks_claude(monkeypatch, settings):
    def boom(*a, **k):
        raise AssertionError("Claude was called for the calendar")
    stored(monkeypatch)
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 19, 7, 30))
    assert brief.calendar_today(settings, asker=boom) == "8:00-9:00 AM Dentist; 4:00-5:30 PM Robotics meeting (Room 204)"
    cw.run_fetch(now=T(11, 23, 7, 0))  # read again on the 23rd (a 304: nothing changed)
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 23, 7, 30))
    assert brief.calendar_today(settings, asker=boom) == "all day: Spring concert day"
    assert cw.today_line(now=T(11, 21, 7, 30)) == "no events"


def test_a_calendar_not_read_for_a_day_is_not_reported_as_empty(monkeypatch):
    stored(monkeypatch)
    line = cw.today_line(now=T(11, 22, 7, 30))
    assert line.startswith("not read since Thu 7:30 AM") and "may be missing" in line


def test_the_brief_line_is_capped_and_all_day_events_come_first(monkeypatch):
    many = [ev(f"DTSTART;TZID=America/Los_Angeles:20261119T{9 + i:02d}0000", f"UID:m{i}", f"SUMMARY:Meeting number {i}")
            for i in range(10)] + [ev("DTSTART;VALUE=DATE:20261119", "UID:ad", "SUMMARY:Spirit day")]
    stored(monkeypatch, ics(*many))
    line = cw.today_line(now=T(11, 19, 8))
    assert line.startswith("all day: Spirit day; 9:00 AM Meeting number 0") and "+3 more" in line and len(line) <= 400


def test_without_a_calendar_the_brief_keeps_its_old_connector_path(settings):
    assert not cw.configured() and brief.calendar_today(settings, asker=lambda *a, **k: 1 / 0) == ""


# ---- the core ------------------------------------------------------------------------------------------------

def test_register_core_adds_the_jobs_and_timers_once_and_the_plugin_loads(settings, monkeypatch):
    assert "waggle.calendar_watch" in plugins.MODULES
    report = cw.register_core(now=T(11, 19, 12))
    assert report["jobs"] == ["calendar.fetch", "calendar.headsup"]
    assert report["timers"] == ["calendar.fetch", "calendar.headsup"]
    fetch, head = jobs.handler("calendar.fetch"), jobs.handler("calendar.headsup")
    assert fetch.detached and fetch.timeout_s and not head.detached
    got = {t["id"]: t for t in timers.list(origin="calendar")}
    assert (got["calendar.fetch"]["spec"], got["calendar.fetch"]["catch_up"]) == ("*/15 6-22 * * *", "latest")
    assert (got["calendar.headsup"]["spec"], got["calendar.headsup"]["catch_up"]) == ("*/5 * * * *", "skip")
    first = {k: v["next_run"] for k, v in got.items()}
    cw.register_core(now=T(11, 19, 13))
    assert {t["id"]: t["next_run"] for t in timers.list(origin="calendar")} == first
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.calendar_watch")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    assert plugins.load(record=False) == {"loaded": ["waggle.calendar_watch"], "failed": {}}


def test_the_heads_up_job_runs_through_the_core_job_machinery(settings, monkeypatch):
    stored(monkeypatch)
    cw.register_core(now=T(11, 19, 12))
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 19, 15, 40))
    owner = jobs.new_owner("test")
    jobs.enqueue("calendar.headsup", {}, now=T(11, 19, 15, 40))
    job = jobs.claim(owner, kinds=["calendar.headsup"], now=T(11, 19, 15, 40))
    result = jobs.invoke(jobs.handler("calendar.headsup"), job)
    jobs.complete(job["id"], owner, result, now=T(11, 19, 15, 40))
    assert result["sent"] == 1 and len(texts()) == 1


# ---- login, the real fetcher, secrecy, the command line ------------------------------------------------------

def answers(*replies):
    it = iter(replies)
    return lambda prompt: next(it)


def test_login_checks_the_address_stores_it_only_in_credential_manager_and_reads_the_calendar(settings, monkeypatch):
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 19, 12))
    feed = Feed(BODY)
    code, message = cw.login(ask_user=answers(""), ask_secret=lambda p: "  " + SECRET + "\n", fetch=feed)
    assert (code, message) == (0, "ok: read calendar [personal], 3 events in the next 14 days")
    assert wincred.read(cw.CRED_PREFIX + "personal") == ("personal", SECRET)
    assert cw.calendars() == ["personal"]
    assert SECRET not in data_path("config.json").read_text(encoding="utf-8")
    assert cw.upcoming(14, now=T(11, 19, 12))[0]["summary"] == "Robotics meeting"  # `today` works right away
    code, _ = cw.login(ask_user=answers("School"), ask_secret=lambda p: SECRET.replace("owner", "school"), fetch=feed)
    assert code == 0 and cw.calendars() == ["personal", "school"]  # a second calendar by name; the first is kept


def test_login_refuses_bad_input_with_plain_messages_that_never_hold_the_address(settings):
    assert cw.login(ask_user=answers("Bad Name!"), ask_secret=lambda p: SECRET)[0] == 1
    code, msg = cw.login(ask_user=answers(""), ask_secret=lambda p: "file:///c:/secret.txt")
    assert code == 1 and "https://" in msg
    assert cw.login(ask_user=answers(""), ask_secret=lambda p: "")[0] == 1
    for kind in ("http 404", "timeout", "network", "not a calendar"):
        def bad(url, etag="", lm="", kind=kind):
            raise cw.CalError(kind)
        code, msg = cw.login(ask_user=answers(""), ask_secret=lambda p: SECRET, fetch=bad)
        assert code == 1 and msg.startswith("could not read the calendar") and SECRET not in msg
    code, msg = cw.login(ask_user=answers(""), ask_secret=lambda p: SECRET,
                         fetch=lambda *a: (200, "<html></html>", "", ""))
    assert code == 1 and "could not read the calendar" in msg
    assert cw.calendars() == [] and wincred.read(cw.CRED_PREFIX + "personal") is None  # nothing stored on failure


def test_the_real_fetcher_maps_errors_to_short_kinds_and_refuses_non_https(monkeypatch):
    def http_error(req, timeout=0):
        raise urllib.error.HTTPError(SECRET, 404, "Not Found", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", http_error)
    with pytest.raises(cw.CalError) as e:
        cw.fetch_url(SECRET)
    assert e.value.kind == "http 404" and e.value.__cause__ is None and e.value.__suppress_context__
    assert SECRET not in repr(e.value) and SECRET not in str(e.value)

    def down(req, timeout=0):
        raise urllib.error.URLError(OSError(11001, "getaddrinfo failed " + SECRET))
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(cw.CalError) as e2:
        cw.fetch_url(SECRET)
    assert e2.value.kind == "network" and SECRET not in repr(e2.value) and e2.value.__cause__ is None
    with pytest.raises(cw.CalError):
        cw.fetch_url("file:///c:/windows/win.ini")
    with pytest.raises(cw.CalError):
        cw.fetch_url("http://calendar.example/x.ics")
    assert cw.normalize_url("webcal://calendar.example/a/basic.ics") == "https://calendar.example/a/basic.ics"

    sent = {}

    class Resp:
        headers = {"ETag": '"abc"', "Last-Modified": "Wed, 18 Nov 2026 10:00:00 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return BODY.encode()

    def ok(req, timeout=0):
        sent.update({k.lower(): v for k, v in req.header_items()})
        sent["timeout"] = timeout
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", ok)
    status, text, etag, lm = cw.fetch_url(SECRET, '"old"', "Tue, 17 Nov 2026 10:00:00 GMT", timeout=12)
    assert (status, text, etag) == (200, BODY, '"abc"') and lm.startswith("Wed")
    assert sent["if-none-match"] == '"old"' and sent["timeout"] == 12 and "if-modified-since" in sent


def test_the_address_never_appears_in_logs_events_status_errors_stores_or_output(settings, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    feed = set_up(monkeypatch, BODY)
    cw.run_fetch(now=T(11, 19, 7, 30))
    feed.fail = "http 404"
    for i in range(4):
        cw.run_fetch(now=T(11, 19, 8) + 900 * i)
    feed.fail = None
    cw.fetch_job(now=T(11, 19, 15, 40), fetch=feed)
    cw.register_core(now=T(11, 19, 12))
    cw.main(["status"])
    cw.main(["today"])

    def down(*a):
        raise cw.CalError("network")
    cw.login(ask_user=answers("other"), ask_secret=lambda p: SECRET, fetch=down)
    everything = [caplog.text, capsys.readouterr().out, json.dumps(cw.status(now=T(11, 19, 12)), default=str),
                  json.dumps(cw.today(now=T(11, 19, 12)), default=str), repr(cw.CalError("http 404"))]
    home = data_path("x").parent
    everything += [p.read_text(encoding="utf-8", errors="replace") for p in home.rglob("*")
                   if p.is_file() and (p.suffix in (".jsonl", ".json", ".log", ".txt") or p.name.startswith("config.json"))]
    for db in (data_path(cw.DB_NAME), store.db_path()):
        everything.append(db.read_bytes().decode("latin-1"))
    everything += [json.dumps(r, default=str) for r in outbox.list(limit=1000)]
    for text in everything:
        assert SECRET not in text and "private-0123456789abcdef" not in text
    assert wincred.read(cw.CRED_PREFIX + "personal")[1] == SECRET  # the one place it is held


def test_status_and_today_on_the_command_line(settings, monkeypatch, capsys):
    assert cw.main(["today"]) == 1 and "login" in capsys.readouterr().out
    stored(monkeypatch)
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 19, 12))
    assert cw.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["calendars"][0]["name"] == "personal" and st["calendars"][0]["address_stored"]
    assert st["headsup_min"] == 30
    assert cw.main(["today"]) == 0
    out = capsys.readouterr().out
    assert "Robotics meeting (Room 204)" in out and "Dentist" in out


def test_run_dry_run_on_the_command_line_prints_what_it_would_send(settings, monkeypatch, capsys):
    set_up(monkeypatch, BODY)
    monkeypatch.setattr(cw.time, "time", lambda: T(11, 19, 15, 45))
    assert cw.main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "WOULD SEND:\nIn 15 min: Robotics meeting (Room 204), 4:00-5:30 PM." in out
    assert texts() == [] and not data_path(cw.DB_NAME).exists()
