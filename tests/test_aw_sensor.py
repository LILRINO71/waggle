"""ActivityWatch sensor with ActivityWatch mocked: derived rows only, nothing raw stored or logged, AFK cut out,
quiet no-op when it is not running, and the feed into the activity model and the miner."""
import datetime as dt

from waggle import activity, aw_sensor, miner
from waggle.config import data_path
from waggle.core import jobs, timers

NOW = 1_790_000_000.0
DAY = 86400.0

BUCKETS = {"aw-watcher-window_pc": {"type": "currentwindow", "last_updated": "2026-01-01T00:00:00+00:00"},
           "aw-watcher-afk_pc": {"type": "afkstatus", "last_updated": "2026-01-01T00:00:00+00:00"},
           "aw-watcher-web-chrome": {"type": "web.tab.current", "last_updated": "2026-01-01T00:00:00+00:00"}}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def ev(ts, dur, **data):
    return {"id": 1, "timestamp": iso(ts), "duration": dur, "data": data}


class FakeAw:
    """A stand-in for aw_sensor._get: pass it as get=. Event lists are newest first, as ActivityWatch returns them."""

    def __init__(self, win=(), afk=(), web=(), up=True, buckets=None):
        self.win, self.afk, self.web, self.up = list(win), list(afk), list(web), up
        self.buckets = BUCKETS if buckets is None else buckets
        self.calls = []

    def __call__(self, path, params=None, timeout=4.0):
        self.calls.append(path)
        if not self.up:
            raise aw_sensor.AwDown("down")
        if path == "/api/0/buckets/":
            return self.buckets
        data = {"aw-watcher-window_pc": self.win, "aw-watcher-afk_pc": self.afk,
                "aw-watcher-web-chrome": self.web}
        for name, evs in data.items():
            if path.endswith(f"/buckets/{name}/events"):
                return evs[:int((params or {}).get("limit", 100))]
        raise aw_sensor.AwDown("404")


def test_classify_titles_hosts_apps_and_workstream_labels():
    c = aw_sensor.classify
    assert c("code", "TeleOp.java - ftc-base - Visual Studio Code")[0] == "ftc"
    assert c("code", "main.py - something - Visual Studio Code")[0] == "coding"
    assert c("chrome", "Home", "onshape.com") == ("onshape", "", "onshape.com")
    assert c("chrome", "Funny cats - YouTube")[0] == "video"        # a title that says YouTube
    assert c("chrome", "x", "youtube.com") == ("video", "", "youtube.com")
    assert c("chrome", "Calculus homework - Google Docs")[0] == "school"
    assert c("chrome", "Episode 4 sub - Crunchyroll")[0] == "anime"
    assert c("steam", "Steam")[0] == "games"
    assert c("chrome", "A page", "unknown-site.example")[2] == ""  # an unknown host is never remembered
    assert c("explorer", "")[0] == ""
    labels = {"waggle": {"waggle", "waggle"}, "ftc-base": {"ftc-base", "ftc base"}}
    assert c("code", "activity.py - waggle-wt-aw - Visual Studio Code", "", labels)[1] == "waggle"
    assert c("code", "chassis.py - ftc-base - VS Code", "", labels)[1] == "ftc-base"
    assert aw_sensor.app_name("Chrome.exe") == "chrome" and aw_sensor.host_of("https://www.A.com/x?y=1") == "a.com"


def test_derive_cuts_afk_out_and_keeps_no_raw_text():
    win = [ev(NOW, 600, app="code.exe", title="SECRET-TITLE ftc - Visual Studio Code")]
    afk = [ev(NOW + 200, 200, status="afk"), ev(NOW, 200, status="not-afk")]
    rows = aw_sensor.derive(win, afk, [], None, salt="s", now=NOW + 700)
    active = sorted((r["start"], r["secs"]) for r in rows if not r["afk"])
    assert active == [(NOW, 200.0), (NOW + 400, 200.0)]
    assert [(r["start"], r["secs"]) for r in rows if r["afk"]] == [(NOW + 200, 200.0)]
    assert all("SECRET" not in repr(r) for r in rows)
    assert {r["project"] for r in rows if not r["afk"]} == {"ftc"}
    assert rows[0]["tid"] != aw_sensor.title_id("SECRET-TITLE ftc - Visual Studio Code", "other-salt")


def test_poll_stores_derived_rows_and_never_raw_titles_or_urls(settings):
    win = [ev(NOW + 700, 300, app="chrome.exe", title="HOMEWORK-SECRET-42 - Google Docs"),
           ev(NOW + 300, 400, app="chrome.exe", title="whatever-secret-video - YouTube"),
           ev(NOW, 300, app="code.exe", title="chassis.py - ftc-base - Visual Studio Code")]
    web = [ev(NOW + 300, 400, url="https://www.youtube.com/watch?v=URLSECRET99", title="t"),
           ev(NOW + 700, 300, url="https://docs.google.com/document/d/DOCSECRET77", title="t")]
    afk = [ev(NOW, 1000, status="not-afk")]
    get = FakeAw(win, afk, web)
    out = aw_sensor.poll(settings, now=NOW + 1000, get=get, labels={"ftc-base": {"ftc-base", "ftc base"}})
    assert out["slices"] == 3 and out["active_min"] == 16.7 and out["buckets"] == {"window": True, "afk": True, "web": 1}
    rows = aw_sensor.slices_since(NOW - 1)
    by_app = {(r["app"], r["project"]) for r in rows}
    assert by_app == {("code", "ftc"), ("chrome", "video")} | {("chrome", "school")}
    assert [r["host"] for r in rows if r["project"] == "video"] == ["youtube.com"]
    assert [r["ws"] for r in rows if r["app"] == "code"] == ["ftc-base"]
    blob = b"".join(p.read_bytes() for p in data_path("").iterdir() if p.is_file())
    for secret in (b"HOMEWORK-SECRET", b"whatever-secret", b"URLSECRET99", b"DOCSECRET77", b"docs.google.com"):
        assert secret not in blob, secret


def test_poll_is_idempotent_and_reads_from_the_watermark(settings):
    win = [ev(NOW, 300, app="code.exe", title="a ftc")]
    get = FakeAw(win, [])
    aw_sensor.poll(settings, now=NOW + 400, get=get, labels={})
    first = aw_sensor.slices_since(0)
    aw_sensor.poll(settings, now=NOW + 700, get=get, labels={})
    assert aw_sensor.slices_since(0) == first
    bigger = [ev(NOW, 600, app="code.exe", title="a ftc")]  # the heartbeat merged more time into the same event
    aw_sensor.poll(settings, now=NOW + 800, get=FakeAw(bigger, []), labels={})
    assert [r["secs"] for r in aw_sensor.slices_since(0)] == [600.0]


def test_quiet_noop_when_activitywatch_is_down_off_or_has_no_window_bucket(settings):
    assert aw_sensor.poll(settings, now=NOW, get=FakeAw(up=False))["skipped"] == "ActivityWatch is not running"
    assert aw_sensor.poll(settings, now=NOW)["skipped"] == "ActivityWatch is not running"  # the default _get, no AW
    assert "no window bucket" in aw_sensor.poll(settings, now=NOW, get=FakeAw(buckets={}))["skipped"]
    settings.activitywatch_enabled = False
    fake = FakeAw([ev(NOW, 60, app="code.exe", title="x")])
    assert "off" in aw_sensor.poll(settings, now=NOW, get=fake)["skipped"] and fake.calls == []
    assert aw_sensor.slices_since(0) == [] and not aw_sensor.available(settings, fake)


def test_current_window_is_in_memory_only_and_stale_or_missing_is_none():
    now = NOW + 100
    get = FakeAw([ev(now - 6, 5, app="chrome.exe", title="Some Page")], [ev(now - 6, 5, status="not-afk")],
                 [ev(now - 6, 5, url="https://www.youtube.com/x", title="t")])
    cur = aw_sensor.current(get, now)
    assert cur == {"app": "chrome", "title": "Some Page", "host": "youtube.com", "afk": False}
    assert aw_sensor.current(get, now + 1000) is None                     # nothing recent
    assert aw_sensor.current(FakeAw(up=False), now) is None
    afk = FakeAw([ev(now - 6, 5, app="code.exe", title="t")], [ev(now - 6, 5, status="afk")])
    assert aw_sensor.current(afk, now)["afk"] is True


def test_the_poll_job_obeys_quiet_hours_and_the_core_registers_the_timer(settings):
    out = aw_sensor.register_core()
    assert out == {"jobs": ["activitywatch.poll"], "timers": ["activitywatch.poll"]}
    assert aw_sensor.register_core() == out
    t = timers.get("activitywatch.poll")
    assert t["kind"] == "interval" and t["spec"] == "10m" and t["catch_up"] == "skip"
    assert jobs.handler("activitywatch.poll") is not None
    ok = aw_sensor.poll_job({}, settings=settings, now=NOW, get=FakeAw(up=False))
    assert ok["skipped"] == "ActivityWatch is not running"
    settings.quiet_hours = [0, 24]
    assert aw_sensor.poll_job({}, settings=settings, now=NOW, get=FakeAw(up=False)) == {"skipped": "quiet hours"}


def test_doctor_check(settings, monkeypatch):
    assert aw_sensor.doctor_check(settings)[0] == "warn"
    monkeypatch.setattr(aw_sensor, "_get", FakeAw([ev(NOW, 5, app="code.exe", title="t")]))
    assert aw_sensor.doctor_check(settings)[0] == "ok"
    settings.activitywatch_enabled = False
    assert aw_sensor.doctor_check(settings) == ("ok", "ActivityWatch sensing is switched off (activitywatch_enabled)", "")


# ---- the feed into the activity model and the miner ---------------------------------------------------------------

def row(day, start_s, secs, app, project="", ws="", afk=0):
    return {"start": NOW + day * DAY + start_s, "secs": secs, "app": app, "project": project, "ws": ws, "host": "",
            "tid": "", "afk": afk}


def test_activity_counts_focused_minutes_for_workstreams_and_ignores_leisure_and_afk():
    rows = [row(-0.5, 0, 1800, "code", "ftc"), row(-0.5, 1800, 600, "chrome", "school"),
            row(-0.5, 2400, 3600, "chrome", "video"), row(-0.5, 6000, 3600, "", "afk", afk=1),
            row(-0.5, 9600, 1200, "code", "coding", ws="waggle"), row(-0.5, 10800, 900, "code", "coding")]
    ranked = {w["key"]: w for w in activity.build([], aw=rows, now=NOW)}
    assert set(ranked) == {"ftc", "school", "waggle"}                      # video, afk and plain coding: no workstream
    assert ranked["ftc"]["score"] > ranked["school"]["score"] > 0.5 * 600 / 600 * 0.5
    assert ranked["ftc"]["evidence"]["aw_minutes_7d"] == 30 and ranked["waggle"]["evidence"]["aw_minutes_7d"] == 20
    older = [row(-3, 0, 600, "code", "ftc")]
    assert activity.build([], aw=older, now=NOW)[0]["score"] < ranked["school"]["score"]


def test_refresh_reads_the_stored_rows_unless_switched_off(settings):
    aw_sensor.save([row(-0.1, 0, 1200, "code", "ftc")], now=NOW)
    snap = activity.refresh(settings, now=NOW, claude_dir=settings.vault, repos={}, save=False)
    assert [w["key"] for w in snap["workstreams"]] == ["ftc"] and snap["stats"]["aw_slices"] == 1
    settings.activitywatch_enabled = False
    assert activity.refresh(settings, now=NOW, claude_dir=settings.vault, repos={}, save=False)["workstreams"] == []


def routine(day, base=0, apps=("code", "chrome:onshape", "vls")):
    rows, t = [], base
    for a in apps:
        app, _, proj = a.partition(":")
        rows.append(row(day, t, 120, app, proj))
        t += 130
    return rows


def test_a_repeated_app_sequence_becomes_an_app_sequence_candidate():
    rows = routine(0) + routine(1) + routine(2, 3600) + routine(2, 0)
    seqs = aw_sensor.sequences(rows)
    assert "code > chrome:onshape > vls" in seqs
    found = miner.mine([], chat=[], now=NOW + 3 * DAY, aw=rows)
    c = next(x for x in found if x["kind"] == "app_sequence")
    assert c["label"] == "code > chrome:onshape > vls" and c["recipe"] is None
    assert c["evidence"]["count"] >= 3 and c["evidence"]["days"] == 3 and c["evidence"]["sources"] == ["activitywatch"]
    assert miner.mine([], chat=[], now=NOW + 3 * DAY, aw=routine(0) + routine(1)) == []  # two times is not a habit
    miner.save(found, NOW + 3 * DAY)
    assert miner.candidates("found")[0]["kind"] == "app_sequence"           # saved, never given a shadow run


def test_sequences_skip_noise_leisure_afk_and_long_gaps():
    rows = [row(0, 0, 100, "code"), row(0, 110, 100, "explorer"), row(0, 220, 100, "chrome", "onshape"),
            row(0, 330, 100, "vls")]
    assert list(aw_sensor.sequences(rows)) == ["code > chrome:onshape > vls"]
    assert aw_sensor.sequences([row(0, 0, 100, "code"), row(0, 110, 100, "chrome", "video"),
                                row(0, 220, 100, "vls")]) == {}
    assert aw_sensor.sequences([row(0, 0, 100, "code"), row(0, 5000, 100, "chrome"), row(0, 5200, 100, "vls")]) == {}
    assert aw_sensor.sequences([row(0, 0, 100, "code"), row(0, 110, 100, "", "afk", afk=1),
                                row(0, 220, 100, "chrome"), row(0, 330, 100, "vls")])["code > chrome:web > vls"]


def test_the_sensor_logs_nothing_and_calls_no_model():
    import inspect
    src = inspect.getsource(aw_sensor)
    assert "events.log" not in src and "print(" not in src.split("def main")[0] and "logging" not in src
    imports = [ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    assert not any(x in ln for ln in imports for x in ("claude_cli", "chat_engine", "cascade", "ollama", "budget"))


def test_afk_now_reads_only_the_status_and_is_none_when_it_cannot_say():
    now = NOW
    on = FakeAw(afk=[ev(now - 30, 25, status="not-afk")])
    assert aw_sensor.afk_now(on, now) is False
    away = FakeAw(afk=[ev(now - 30, 25, status="afk")])
    assert aw_sensor.afk_now(away, now) is True
    assert aw_sensor.afk_now(FakeAw(afk=[ev(now - 900, 25, status="not-afk")]), now) is None  # stale heartbeat
    assert aw_sensor.afk_now(FakeAw(afk=[]), now) is None
    assert aw_sensor.afk_now(FakeAw(up=False), now) is None
    assert aw_sensor.afk_now(FakeAw(buckets={"w": {"type": "currentwindow"}}), now) is None  # no AFK bucket
