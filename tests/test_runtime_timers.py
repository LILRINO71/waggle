from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from waggle.core import cron, events, jobs, store, timers


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(jobs, "_handlers", {})
    yield
    store.close_all()


def local(y, mo, d, h=0, mi=0):
    """Epoch of a local wall time (dates chosen away from daylight-saving changes)."""
    return datetime(y, mo, d, h, mi).timestamp()


def _zoneinfo_has_data():
    try:
        import zoneinfo
        zoneinfo.ZoneInfo("America/Los_Angeles")
        return True
    except Exception:
        return False


class USPacific(tzinfo):
    """US Pacific rules since 2007, with PEP 495 fold: DST from the second Sunday of March 02:00 to the first
    Sunday of November 02:00. Stands in for zoneinfo where the machine has no zone data."""
    STD, DST = timedelta(hours=-8), timedelta(hours=-7)

    @staticmethod
    def _bounds(year):
        march = datetime(year, 3, 8)
        start = march + timedelta(days=(6 - march.weekday()) % 7, hours=2)
        nov = datetime(year, 11, 1)
        end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=2)
        return start, end

    def _is_dst(self, dt):
        start, end = self._bounds(dt.year)
        wall = dt.replace(tzinfo=None, fold=0)
        if start <= wall < start + timedelta(hours=1):
            return dt.fold == 1
        if end - timedelta(hours=1) <= wall < end:
            return dt.fold == 0
        return start <= wall < end

    def utcoffset(self, dt):
        return self.DST if self._is_dst(dt) else self.STD

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "PDT" if self._is_dst(dt) else "PST"

    def fromutc(self, dt):
        start, end = self._bounds(dt.year)
        utc = dt.replace(tzinfo=None)
        if start - self.STD <= utc < end - self.DST:
            return (utc + self.DST).replace(tzinfo=self)
        fold = 1 if end - self.DST <= utc < end - self.DST + timedelta(hours=1) else 0
        return (utc + self.STD).replace(tzinfo=self, fold=fold)


PT = USPacific()


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


# ---- cron parser -------------------------------------------------------------------------------------------

def test_cron_parses_fields_names_and_macros():
    c = cron.parse("*/15 9-17/4 1,15 jan-mar mon-fri")
    assert c.minutes == (0, 15, 30, 45) and c.hours == (9, 13, 17)
    assert c.doms == {1, 15} and c.months == {1, 2, 3} and c.dows == {1, 2, 3, 4, 5}
    assert cron.parse("0 0 * * 7").dows == {0}
    assert cron.parse("@daily").hours == (0,)
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *", "x * * * *", "5-1 * * * *"):
        with pytest.raises(ValueError):
            cron.parse(bad)


def test_cron_next_after_in_local_time():
    t = local(2026, 6, 10, 7, 29)
    assert cron.next_after("30 7 * * *", t) == local(2026, 6, 10, 7, 30)
    assert cron.next_after("30 7 * * *", local(2026, 6, 10, 7, 30)) == local(2026, 6, 11, 7, 30)
    assert cron.next_after("0 9 * * mon", t) == local(2026, 6, 15, 9, 0)  # 2026-06-10 is a Wednesday
    assert cron.next_after("0 0 1 * *", t) == local(2026, 7, 1)
    assert cron.next_after("0 12 29 2 *", t) == local(2028, 2, 29, 12)
    assert cron.next_after("0 0 31 2 *", t) is None
    # Day-of-month OR day-of-week when both are restricted.
    assert cron.next_after("0 8 13 * fri", local(2026, 6, 10)) == local(2026, 6, 12, 8)


def test_cron_daily_across_spring_forward_in_pacific_time():
    # 2026-03-08 02:00 PST jumps to 03:00 PDT; 02:30 does not exist that day.
    t = utc(2026, 3, 7, 11, 0)  # 03:00 PST on 2026-03-07
    runs = []
    for _ in range(3):
        t = cron.next_after("30 2 * * *", t, PT)
        runs.append(t)
    assert runs[0] == utc(2026, 3, 7, 10, 30) + 86400  # 2026-03-08 10:30 UTC = 03:30 PDT, the only run that day
    assert datetime.fromtimestamp(runs[0], PT).strftime("%m-%d %H:%M %Z") == "03-08 03:30 PDT"
    assert datetime.fromtimestamp(runs[1], PT).strftime("%m-%d %H:%M %Z") == "03-09 02:30 PDT"
    assert runs[2] - runs[1] == 86400
    nine = [cron.next_after("0 9 * * *", utc(2026, 3, 6, 18) + i * 86400, PT) for i in range(3)]
    assert [datetime.fromtimestamp(x, PT).hour for x in nine] == [9, 9, 9]
    assert nine[2] - nine[1] == 86400 and nine[1] - nine[0] == 23 * 3600


def test_cron_daily_across_fall_back_in_pacific_time():
    # 2026-11-01 02:00 PDT falls back to 01:00 PST; 01:30 happens twice and must run once.
    t = utc(2026, 10, 31, 9, 0)  # 02:00 PDT on 2026-10-31
    runs = []
    for _ in range(3):
        t = cron.next_after("30 1 * * *", t, PT)
        runs.append(t)
    assert runs[0] == utc(2026, 11, 1, 8, 30)  # 01:30 PDT, first occurrence
    assert runs[1] == utc(2026, 11, 2, 9, 30)  # next day 01:30 PST: the repeated 01:30 did not run
    assert runs[2] - runs[1] == 86400
    # Asked from inside the repeated hour, the next run is tomorrow, not the 01:30 that already passed.
    assert cron.next_after("30 1 * * *", utc(2026, 11, 1, 9, 10), PT) == utc(2026, 11, 2, 9, 30)
    nine = [cron.next_after("0 9 * * *", utc(2026, 10, 30, 18) + i * 86400, PT) for i in range(3)]
    assert [datetime.fromtimestamp(x, PT).hour for x in nine] == [9, 9, 9]
    assert nine[1] - nine[0] == 25 * 3600


@pytest.mark.skipif(not _zoneinfo_has_data(), reason="zoneinfo has no time zone data on this machine")
def test_cron_daylight_saving_with_real_zoneinfo():
    import zoneinfo
    la = zoneinfo.ZoneInfo("America/Los_Angeles")
    assert cron.next_after("30 2 * * *", utc(2026, 3, 7, 11), la) == utc(2026, 3, 8, 10, 30)
    assert cron.next_after("30 1 * * *", utc(2026, 11, 1, 8, 30), la) == utc(2026, 11, 2, 9, 30)


def test_daily_timer_fires_once_per_day_through_both_dst_edges(monkeypatch):
    monkeypatch.setattr(timers, "_resolve_tz", lambda name: PT if name == "America/Los_Angeles" else None)
    timers.add("brief", "cron", "30 1 * * *", "brief.run", tz="America/Los_Angeles", now=utc(2026, 10, 30, 12))
    fired_days = []
    t = utc(2026, 10, 30, 12)
    while t < utc(2026, 11, 3, 12):
        for jid in timers.fire_due(now=t):
            fired_days.append(datetime.fromtimestamp(jobs.get(jid)["payload"]["scheduled_ts"], PT).strftime("%d %H:%M"))
        t += 600
    assert fired_days == ["31 01:30", "01 01:30", "02 01:30", "03 01:30"]


def test_unknown_zone_falls_back_to_local_time_with_an_event(monkeypatch):
    monkeypatch.setattr(timers, "_resolve_tz", lambda name: None)
    timers.add("t", "cron", "0 9 * * *", "k", tz="Mars/Olympus", now=local(2026, 6, 10, 8))
    assert timers.get("t")["next_run"] == local(2026, 6, 10, 9)
    assert timers.get("t")["tz_effective"] == "local"
    assert events.query("timer.tz_fallback")[0]["data"] == {"tz": "Mars/Olympus", "using": "local time"}


# ---- add / once / housekeeping -----------------------------------------------------------------------------

def test_add_is_an_idempotent_upsert():
    now = local(2026, 6, 10, 8)
    timers.add("brief", "cron", "30 7 * * *", "brief.run", {"a": 1}, catch_up="latest", now=now)
    first = timers.get("brief")
    assert first["next_run"] == local(2026, 6, 11, 7, 30) and first["enabled"]
    timers.disable("brief", now=now)
    timers.add("brief", "cron", "30 7 * * *", "brief.run", {"a": 2}, catch_up="latest", now=now + 3600)
    again = timers.get("brief")
    assert again["next_run"] == first["next_run"] and not again["enabled"] and again["payload"] == {"a": 2}
    timers.add("brief", "cron", "0 8 * * *", "brief.run", now=now + 60)
    assert timers.get("brief")["next_run"] == local(2026, 6, 11, 8)
    assert len(timers.list()) == 1
    with pytest.raises(ValueError):
        timers.add("x", "cron", "bad", "k")
    with pytest.raises(ValueError):
        timers.add("x", "weekly", "1", "k")
    with pytest.raises(ValueError):
        timers.add("x", "interval", "5m", "k", catch_up="sometimes")


def test_interval_specs():
    assert timers.interval_seconds("90") == 90
    assert timers.interval_seconds("5m") == 300
    assert timers.interval_seconds("2h") == 7200
    assert timers.interval_seconds(3600) == 3600
    for bad in ("0", "-5", "5x", ""):
        with pytest.raises(ValueError):
            timers.interval_seconds(bad)


def test_on_time_cron_fires_once_with_payload():
    now = local(2026, 6, 10, 7)
    timers.add("brief", "cron", "30 7 * * *", "brief.run", {"chat": "c"}, catch_up="latest", now=now)
    assert timers.fire_due(now=local(2026, 6, 10, 7, 29)) == []
    fired = timers.fire_due(now=local(2026, 6, 10, 7, 30) + 3)
    assert len(fired) == 1
    job = jobs.get(fired[0])
    assert job["kind"] == "brief.run" and job["idem_key"] == f"timer:brief:{timers.fmt_ts(local(2026, 6, 10, 7, 30))}"
    assert job["payload"] == {"chat": "c", "scheduled_ts": local(2026, 6, 10, 7, 30), "late_s": 3.0,
                              "timer_id": "brief"}
    assert timers.fire_due(now=local(2026, 6, 10, 7, 31)) == []
    t = timers.get("brief")
    assert t["next_run"] == local(2026, 6, 11, 7, 30) and t["last_run"] == local(2026, 6, 10, 7, 30)
    assert t["last_job"] == fired[0]
    assert [e["kind"] for e in events.query("timer.*") if e["kind"] != "timer.added"] == ["timer.fired"]


def test_once_fires_once_then_turns_off():
    at = local(2026, 6, 10, 9)
    tid = timers.once(at, "remind", {"text": "stretch"})
    assert tid.startswith("once:")
    assert timers.fire_due(now=at - 1) == []
    fired = timers.fire_due(now=at + 1)
    assert len(fired) == 1 and jobs.get(fired[0])["payload"]["text"] == "stretch"
    t = timers.get(tid)
    assert not t["enabled"] and t["next_run"] is None
    assert timers.fire_due(now=at + 3600) == []
    assert timers.enable(tid) is False
    timers.once(at, "remind", {"text": "stretch"}, timer_id=tid)
    assert timers.fire_due(now=at + 7200) == []


def test_interval_keeps_its_phase():
    start = local(2026, 6, 10, 8)
    timers.add("poll", "interval", "10m", "poll.run", now=start)
    assert timers.get("poll")["next_run"] == start + 600
    assert len(timers.fire_due(now=start + 600)) == 1
    assert timers.get("poll")["next_run"] == start + 1200


def test_disable_enable_remove():
    now = local(2026, 6, 10, 8)
    timers.add("t", "cron", "0 9 * * *", "k", now=now)
    assert timers.disable("t", now=now)
    assert timers.fire_due(now=local(2026, 6, 10, 9)) == []
    assert timers.enable("t", now=local(2026, 6, 12, 10))
    assert timers.get("t")["next_run"] == local(2026, 6, 13, 9)
    assert timers.list(enabled=True)[0]["id"] == "t"
    assert timers.remove("t") and timers.get("t") is None and not timers.remove("t")


# ---- catch-up after the PC was off ---------------------------------------------------------------------------

def _overnight_timers(off):
    timers.add("hq", "cron", "*/5 * * * *", "hq.refresh", catch_up="skip", now=off)
    timers.add("brief", "cron", "30 7 * * *", "brief.run", catch_up="latest", max_late_s=3.5 * 3600, now=off)
    timers.add("note", "cron", "0 21 * * *", "note.run", catch_up="latest", now=off)
    timers.add("sync", "interval", "1h", "sync.run", catch_up="all", now=off)
    timers.once(local(2026, 6, 10, 23), "remind", {"text": "call back"}, timer_id="r1")


def test_catch_up_policies_after_a_night_off_until_9am():
    off = local(2026, 6, 10, 22)
    _overnight_timers(off)
    store.set_meta("timers.last_tick", off)
    now = local(2026, 6, 11, 9, 0) + 150  # the 09:00 runs are already 150 s late
    report = timers.catch_up(now=now)
    assert report["last_tick"] == off
    by_kind = {}
    for jid in report["fired"]:
        job = jobs.get(jid)
        by_kind.setdefault(job["kind"], []).append(job["payload"])
    assert "hq.refresh" not in by_kind
    assert [p["scheduled_ts"] for p in by_kind["brief.run"]] == [local(2026, 6, 11, 7, 30)]
    assert by_kind["brief.run"][0]["late_s"] == 5400 + 150
    assert "note.run" not in by_kind  # next 21:00 is tonight, nothing was missed
    assert len(by_kind["sync.run"]) == 11  # 23:00 .. 09:00 hourly
    assert by_kind["remind"][0]["late_s"] == 10 * 3600 + 150 and by_kind["remind"][0]["text"] == "call back"
    skipped = {s["timer_id"]: s for s in report["skipped"]}
    assert skipped["hq"]["count"] == 132 and skipped["hq"]["reason"] == "policy skip"
    assert "brief" not in skipped
    assert {x["timer_id"] for x in report["late"]} == {"brief", "sync", "r1"}
    assert len(events.query("timer.late", limit=1000)) == 13  # brief + 11 sync + reminder
    assert events.query("timers.catch_up")[0]["data"]["skipped"] == 132
    assert timers.get("hq")["next_run"] == local(2026, 6, 11, 9, 5)
    assert timers.get("brief")["next_run"] == local(2026, 6, 12, 7, 30)
    assert timers.catch_up(now=now)["fired"] == []  # second pass: nothing twice


def test_latest_is_dropped_after_max_late_s_with_an_event():
    off = local(2026, 6, 10, 22)
    _overnight_timers(off)
    report = timers.catch_up(now=local(2026, 6, 11, 16))
    kinds = [jobs.get(j)["kind"] for j in report["fired"]]
    assert "brief.run" not in kinds and "remind" in kinds
    skipped = {s["timer_id"]: s for s in report["skipped"]}
    assert skipped["brief"]["count"] == 1 and "max_late_s" in skipped["brief"]["reason"]
    assert skipped["brief"]["last"] == local(2026, 6, 11, 7, 30)
    assert any(e["ref"] == "brief" for e in events.query("timer.skipped"))


def test_latest_fires_only_the_most_recent_of_several_missed_runs():
    timers.add("note", "cron", "0 21 * * *", "note.run", catch_up="latest", now=local(2026, 6, 8, 20))
    report = timers.catch_up(now=local(2026, 6, 11, 8))
    assert len(report["fired"]) == 1
    assert jobs.get(report["fired"][0])["payload"]["scheduled_ts"] == local(2026, 6, 10, 21)
    assert report["skipped"][0]["count"] == 2


def test_all_with_max_late_s_and_the_firing_cap():
    timers.add("follow", "interval", "1m", "f", catch_up="all", max_late_s=600, now=local(2026, 6, 10, 0))
    report = timers.catch_up(now=local(2026, 6, 10, 2))
    assert len(report["fired"]) == 11  # only runs at most 10 minutes late: 01:50 .. 02:00
    assert report["skipped"][0]["count"] == 109
    timers.remove("follow")
    timers.add("many", "interval", "1m", "g", catch_up="all", now=local(2026, 6, 10, 3))
    report = timers.catch_up(now=local(2026, 6, 10, 5))
    assert len(report["fired"]) == timers.MAX_FIRE_ALL
    assert report["skipped"][0]["count"] == 120 - timers.MAX_FIRE_ALL
    assert report["skipped"][0]["reason"].startswith("more than")


def test_very_long_outage_of_a_frequent_cron_timer_is_bounded():
    timers.add("tick", "cron", "* * * * *", "tick.run", catch_up="latest", now=local(2026, 5, 1))
    report = timers.catch_up(now=local(2026, 6, 10, 12, 0) + 30)
    assert len(report["fired"]) == 1
    assert jobs.get(report["fired"][0])["payload"]["scheduled_ts"] == local(2026, 6, 10, 12, 0)
    assert report["skipped"][0]["approx"] is True and report["skipped"][0]["count"] >= timers.ENUM_CAP - 1
    assert timers.get("tick")["next_run"] == local(2026, 6, 10, 12, 1)


def test_skip_timer_a_minute_late_still_fires():
    timers.add("hq", "cron", "*/5 * * * *", "hq.refresh", catch_up="skip", now=local(2026, 6, 10, 8, 1))
    fired = timers.fire_due(now=local(2026, 6, 10, 8, 6))
    assert len(fired) == 1 and jobs.get(fired[0])["payload"]["late_s"] == 60


def test_timer_jobs_are_idempotent_even_if_a_timer_is_rewound():
    timers.add("t", "cron", "0 9 * * *", "k", now=local(2026, 6, 10, 8))
    first = timers.fire_due(now=local(2026, 6, 10, 9))
    store.connect().execute("UPDATE timers SET next_run=? WHERE id='t'", (local(2026, 6, 10, 9),))
    assert timers.fire_due(now=local(2026, 6, 10, 9)) == first
    assert len(jobs.list(kind="k")) == 1
