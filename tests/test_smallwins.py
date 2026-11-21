"""Small wins: Wolfram's cloud MCP in the chat brain's config, the FTCScout tool, the seven-day usage reading and
its background defer line, and nightly local backups. Fakes for the network, temp homes for everything."""
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from waggle import backup, budget, chat_session, config, ftcscout, policy, quota
from waggle import waggle_mcp as mcp
from waggle.config import Settings, data_path

H = 3600


# ---- 1. Wolfram in the chat brain's MCP config ---------------------------------------------------------------------

def mcp_servers(settings):
    return json.loads(Path(chat_session.mcp_config("telegram:1", settings)).read_text(encoding="utf-8"))["mcpServers"]


def test_wolfram_is_offered_by_default_as_the_official_keyless_server(settings):
    assert Settings().wolfram_mcp is True
    servers = mcp_servers(settings)
    assert servers["wolfram"] == {"type": "http", "url": "https://agenttools.wolfram.com/mcp"}
    assert "headers" not in servers["wolfram"] and "waggle" in servers  # no key; Waggle's own server stays


def test_wolfram_off_leaves_only_waggles_server_and_no_prompt_line(settings):
    settings.wolfram_mcp = False
    assert list(mcp_servers(settings)) == ["waggle"]
    assert "Wolfram" not in chat_session.system_prompt(settings)
    assert "ftc_team" in chat_session.system_prompt(settings)


def test_the_brain_is_told_to_use_wolfram_for_maths_and_to_send_only_maths(settings):
    text = chat_session.system_prompt(settings)
    assert text.startswith(chat_session.CHAT_SYSTEM)
    assert "mcp__wolfram__WolframAlpha" in text and "never personal text" in text


def test_the_command_line_carries_the_config_and_the_prompt(settings):
    cmd, _model = chat_session.build_cmd("telegram:1", settings, "claude", servers=[])
    cfg = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert "wolfram" in cfg["mcpServers"]
    assert "WolframAlpha" in cmd[cmd.index("--append-system-prompt") + 1]
    settings.wolfram_mcp = False
    cmd, _ = chat_session.build_cmd("telegram:1", settings, "claude", servers=[])
    assert "WolframAlpha" not in cmd[cmd.index("--append-system-prompt") + 1]


def test_a_wolfram_tool_is_allowed_by_the_policy_like_any_mcp_read():
    for tool in ("mcp__wolfram__WolframAlpha", "mcp__wolfram__WolframLanguageEvaluator", "mcp__wolfram__WolframContext"):
        assert policy.decide(tool, {"query": "integrate x^2 from 0 to 3"}).action == policy.ALLOW


# ---- 2. FTCScout ---------------------------------------------------------------------------------------------------

RAW = {"number": 7831, "name": "Titan", "schoolName": "Some High School", "rookieYear": 2013, "website": None,
       "location": {"city": "Town", "state": "CA", "country": "USA"}, "activeSeasons": [2024, 2026],
       "quickStats": {"season": 2026, "count": 3318, "tot": {"value": 12.227, "rank": 2739},
                      "auto": {"value": 4.065, "rank": 2374}, "dc": {"value": 13.58, "rank": 1861},
                      "eg": {"value": 4.71, "rank": 1299}},
       "events": [
           {"eventCode": "B", "season": 2026,
            "stats": {"__typename": "TeamEventStats2026", "rank": 9, "rp": 1.8, "wins": 2, "losses": 3, "ties": 0,
                      "qualMatchesPlayed": 5, "opr": {"totalPointsNp": 10.7, "autoPoints": -2.9, "dcPoints": 13.6}},
            "event": {"name": "Scrimmage 1", "type": "NonAdvancement", "start": "2026-11-07", "end": "2026-11-07",
                      "started": True, "ongoing": False, "finished": True, "location": {"city": "Moreno Valley", "state": "CA"}}},
           {"eventCode": "A", "season": 2026,
            "stats": {"__typename": "TeamEventStats2026", "rank": 11, "rp": 1.2, "wins": 1, "losses": 4, "ties": 0,
                      "qualMatchesPlayed": 5, "opr": {"totalPointsNp": 12.2, "autoPoints": 4.1, "dcPoints": 8.2}},
            "event": {"name": "Scrimmage 0", "type": "NonAdvancement", "start": "2026-10-24", "end": "2026-10-24",
                      "started": True, "ongoing": False, "finished": True, "location": {"city": "Moreno Valley", "state": "CA"}}},
           {"eventCode": "C", "season": 2026, "stats": None,
            "event": {"name": "League Meet 1", "type": "LeagueMeet", "start": "2026-12-05", "end": "2026-12-05",
                      "started": False, "ongoing": False, "finished": False, "location": {"city": "Winchester", "state": "CA"}}}]}


class FakeGraphQL:
    def __init__(self, raw=RAW, errors=None, fail=None):
        self.raw, self.errors, self.fail, self.calls = raw, errors, fail, []

    def __call__(self, query, variables, timeout=0):
        self.calls.append((query, variables))
        if self.fail:
            raise ftcscout.FtcScoutError(self.fail)
        if self.errors:
            return {"errors": self.errors(query)} if callable(self.errors) else {"errors": self.errors}
        return {"data": {"teamByNumber": self.raw}}


def test_the_current_season_is_named_by_the_year_it_starts():
    t = lambda y, m: time.mktime((y, m, 15, 12, 0, 0, 0, 0, -1))  # noqa: E731
    assert ftcscout.default_season(t(2026, 11)) == 2026 and ftcscout.default_season(t(2027, 3)) == 2026
    assert ftcscout.default_season(t(2026, 8)) == 2026 and ftcscout.default_season(t(2026, 7)) == 2025


def test_a_team_is_shaped_into_record_stats_events_and_upcoming(settings):
    d = ftcscout.team(7831, 2026, post=FakeGraphQL())
    assert d["team"]["name"] == "Titan" and d["team"]["where"] == "Town, CA" and d["season"] == 2026
    assert d["record"] == {"wins": 3, "losses": 7, "ties": 0, "qual_matches": 10}
    assert d["opr_stats"]["total"] == {"value": 12.2, "rank": 2739} and d["opr_stats"]["of_teams"] == 3318
    assert [e["code"] for e in d["events"]] == ["A", "B", "C"]  # oldest first
    assert d["events"][0]["opr"] == {"total": 12.2, "auto": 4.1, "teleop": 8.2} and d["events"][0]["record"] == "1-4-0"
    assert [e["code"] for e in d["upcoming"]] == ["C"] and d["upcoming"][0]["status"] == "upcoming"
    assert d["best_event_opr"] == 12.2
    text = ftcscout.format_text(d)
    for part in ("Team 7831 Titan", "3-7-0 over 10 qualification matches", "total 12.2 (#2739)",
                 "rank 11, 1-4-0, RP 1.2, OPR 12.2", "Upcoming: 2026-12-05 League Meet 1"):
        assert part in text, part


def test_the_query_names_the_seasons_own_stats_type_and_sends_the_number_as_a_variable(settings):
    fake = FakeGraphQL()
    ftcscout.team("#7831", 2026, post=fake)
    query, variables = fake.calls[0]
    assert "... on TeamEventStats2026" in query and "events(season: 2026)" in query and variables == {"number": 7831}


def test_answers_are_cached_for_an_hour_per_team_and_season(settings):
    fake = FakeGraphQL()
    t0 = 1_800_000_000
    a = ftcscout.team(7831, 2026, post=fake, now=t0)
    b = ftcscout.team(7831, 2026, post=fake, now=t0 + 3599)
    assert len(fake.calls) == 1 and a["_cached"] is False and b["_cached"] is True and b["team"] == a["team"]
    ftcscout.team(7831, 2025, post=fake, now=t0 + 10)  # another season is another entry
    assert len(fake.calls) == 2
    ftcscout.team(7831, 2026, post=fake, now=t0 + 3601)  # past the hour: asked again
    assert len(fake.calls) == 3
    ftcscout.team(7831, 2026, post=fake, now=t0 + 3602, fresh=True)  # fresh skips the cache
    assert len(fake.calls) == 4
    assert json.loads(data_path("ftcscout_cache.json").read_text(encoding="utf-8"))


def test_a_failed_request_is_not_cached_and_an_old_answer_is_used_when_the_network_is_down(settings):
    t0 = 1_800_000_000
    with pytest.raises(ftcscout.FtcScoutError, match="could not reach"):
        ftcscout.team(7831, 2026, post=FakeGraphQL(fail="could not reach FTCScout (OSError)"), now=t0)
    assert not data_path("ftcscout_cache.json").exists()
    ftcscout.team(7831, 2026, post=FakeGraphQL(), now=t0)
    old = ftcscout.team(7831, 2026, post=FakeGraphQL(fail="could not reach FTCScout"), now=t0 + 7200)
    assert old["_stale"] is True and "saved answer" in ftcscout.format_text(old)


def test_a_validation_error_falls_back_to_the_plain_team_and_event_list(settings):
    def errors(query):
        return [{"message": "Cannot query field", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}] \
            if "TeamEventStats" in query else None
    plain = dict(RAW, quickStats=None, events=[{"eventCode": "A", "season": 2019, "event": RAW["events"][0]["event"]}])
    calls = []

    def post(query, variables, timeout=0):
        calls.append(query)
        bad = errors(query)
        return {"errors": bad} if bad else {"data": {"teamByNumber": plain}}
    d = ftcscout.team(7831, 2022, post=post)
    assert len(calls) == 2 and "TeamEventStats" not in calls[1] and d["events"][0]["code"] == "A"
    assert "record" not in d["events"][0] and d["record"]["qual_matches"] == 0


@pytest.mark.parametrize("number, season, message", [("abc", None, "whole number"), (0, None, "between 1"),
                                                     (7831, 2010, "not available"), (7831, 2099, "not available")])
def test_bad_numbers_and_seasons_are_plain_errors(settings, number, season, message):
    with pytest.raises(ftcscout.FtcScoutError, match=message):
        ftcscout.team(number, season, post=FakeGraphQL())


def test_an_unknown_team_and_a_server_error_are_plain_errors(settings):
    with pytest.raises(ftcscout.FtcScoutError, match="no team 1"):
        ftcscout.team(1, 2026, post=FakeGraphQL(raw=None))
    with pytest.raises(ftcscout.FtcScoutError, match="FTCScout error: boom"):
        ftcscout.team(1, 2026, post=FakeGraphQL(errors=[{"message": "boom"}]))


def test_the_mcp_tool_is_registered_read_only_policy_allowed_and_wraps_the_text_as_untrusted(settings, monkeypatch):
    monkeypatch.setattr(ftcscout, "_post", FakeGraphQL())
    assert "ftc_team" in mcp.TOOLS and policy.WAGGLE_TOOLS["ftc_team"][0] == policy.ALLOW
    assert policy.decide("mcp__waggle__ftc_team", {"number": 7831}).action == policy.ALLOW
    out = mcp.TOOLS["ftc_team"][1]({"number": 7831, "season": 2026})
    assert "untrusted" in out.lower() and "Team 7831 Titan" in out
    bad = json.loads(mcp.TOOLS["ftc_team"][1]({"number": "x"}))
    assert bad["ok"] is False and "whole number" in bad["error"]


def test_the_command_line_prints_text_or_json(settings, monkeypatch, capsys):
    monkeypatch.setattr(ftcscout, "_post", FakeGraphQL())
    assert ftcscout.main(["team", "7831", "--season", "2026"]) == 0
    assert "Team 7831 Titan" in capsys.readouterr().out
    assert ftcscout.main(["team", "7831", "--season", "2026", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["team"]["number"] == 7831
    assert ftcscout.main(["team", "nope"]) == 1 and "whole number" in capsys.readouterr().err


# ---- 3. the seven-day usage ----------------------------------------------------------------------------------------

def both(util5, util7, r5, r7):
    return {"status": "allowed", "resetsAt": r5, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": util5, "resetsAt": r5, "status": "allowed"},
                               "seven_day": {"utilization": util7, "resetsAt": r7, "status": "allowed"}}}


def stored():
    return json.loads(data_path("real_usage.json").read_text(encoding="utf-8"))


def test_the_event_keeps_the_seven_day_window_beside_the_five_hour_one(settings):
    now = 1_795_100_000
    row = quota.note_real_usage(both(0.13, 0.12, now + 2 * H, now + 3 * 86400), now=now)
    assert row["utilization"] == 0.13 and row["seven_day"] == {"utilization": 0.12, "resets_at": now + 3 * 86400,
                                                              "seen_at": now}
    assert stored()["seven_day"]["utilization"] == 0.12
    assert quota.seven_day_reading(now) == row["seven_day"]


def test_a_later_event_without_the_seven_day_window_does_not_lose_it(settings):
    now = 1_795_100_000
    quota.note_real_usage(both(0.13, 0.5, now + 2 * H, now + 86400), now=now)
    only5 = {"status": "allowed", "resetsAt": now + 2 * H, "rateLimitType": "five_hour",
             "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": now + 2 * H}}}
    quota.note_real_usage(only5, now=now + 60)
    assert stored()["utilization"] == 0.2 and stored()["seven_day"]["utilization"] == 0.5


def test_a_seven_day_only_event_is_stored_and_leaves_the_five_hour_reading_alone(settings):
    now = 1_795_100_000
    quota.note_real_usage(both(0.13, 0.1, now + 2 * H, now + 86400), now=now)
    quota.note_real_usage({"rateLimitType": "seven_day", "utilization": 0.9, "resetsAt": now + 86400}, now=now + 30)
    assert stored()["utilization"] == 0.13 and stored()["seven_day"]["utilization"] == 0.9


@pytest.mark.parametrize("bad", ["x", float("nan"), -1, 2.0, True, None])
def test_a_bad_seven_day_value_is_ignored_and_the_five_hour_one_still_counts(settings, bad):
    now = 1_795_100_000
    info = both(0.13, bad, now + 2 * H, now + 86400)
    row = quota.note_real_usage(info, now=now)
    assert row["utilization"] == 0.13 and "seven_day" not in row


def test_an_expired_seven_day_reading_is_not_used(settings):
    now = 1_795_100_000
    quota.note_real_usage(both(0.13, 0.95, now + 2 * H, now + 100), now=now)
    assert quota.seven_day_reading(now + 50)["utilization"] == 0.95
    assert quota.seven_day_reading(now + 101) is None  # the week reset: the old number says nothing
    assert quota.seven_day_block(Settings(), now + 101)[0] is False


def test_status_shows_the_seven_day_use_its_reset_and_the_stop_line(settings):
    now = time.time()
    assert quota.status(settings)["seven_day_pct"] is None
    quota.note_real_usage(both(0.13, 0.42, now + 2 * H, now + 3 * 86400), now=now)
    st = quota.status(settings)
    assert st["seven_day_pct"] == 42.0 and st["seven_day_resets_at"] == pytest.approx(now + 3 * 86400)
    assert st["seven_day_stop_at"] == 0.85


def test_waggle_status_text_has_a_seven_day_line(settings):
    from waggle import engine_commands
    now = time.time()
    quota.note_real_usage(both(0.13, 0.42, now + 2 * H, now + 3 * 86400), now=now)
    text = engine_commands._status(settings)
    assert "Claude 7-day limit: 42% used" in text and "pauses at 85%" in text


def test_background_work_defers_at_the_threshold_and_resumes_below_it(settings):
    now = time.time()
    quota.note_real_usage(both(0.05, 0.84, now + 2 * H, now + 4 * 86400), now=now)
    ok, why, _until = budget.check("sonnet", "objective", settings, now)
    assert ok, why
    quota.note_real_usage(both(0.05, 0.85, now + 2 * H, now + 4 * 86400), now=now)
    ok, why, until = budget.check("sonnet", "objective", settings, now)
    assert not ok and "7-day" in why and "85%" in why
    assert now < until <= now + 6 * H  # asked again within hours, not parked until the reset
    with pytest.raises(budget.Deferred):
        budget.reserve("sonnet", "objective", settings=settings, now=now)
    ok, _why, _ = budget.check("sonnet", "brief", settings, now)  # every background purpose, own allowance or not
    assert not ok


def test_the_threshold_is_a_setting_and_zero_turns_it_off(settings):
    now = time.time()
    quota.note_real_usage(both(0.05, 0.9, now + 2 * H, now + 4 * 86400), now=now)
    assert budget.check("sonnet", "objective", settings, now)[0] is False
    settings.seven_day_stop_at = 0.95
    assert budget.check("sonnet", "objective", settings, now)[0] is True
    settings.seven_day_stop_at = 0
    quota.note_real_usage(both(0.05, 1.2, now + 2 * H, now + 4 * 86400), now=now)
    assert budget.check("sonnet", "objective", settings, now)[0] is True
    assert Settings().seven_day_stop_at == 0.85


def test_chat_is_not_held_back_by_the_seven_day_line(settings):
    now = time.time()
    quota.note_real_usage(both(0.05, 0.99, now + 2 * H, now + 4 * 86400), now=now)
    assert quota.can_escalate("sonnet", settings, now)[0] is True  # only background work waits (budget.check)


# ---- 4. backups ----------------------------------------------------------------------------------------------------

def make_home(root):
    """A small data home with databases (one in WAL mode with unflushed rows), config, lessons and things to skip."""
    home = Path(config.HOME)
    (home / "lessons").mkdir(parents=True, exist_ok=True)
    (home / "kokoro" / "venv").mkdir(parents=True)
    (home / "media" / "chat").mkdir(parents=True)
    (home / "voice_cache").mkdir()
    (home / "kokoro" / "venv" / "big.bin").write_bytes(b"x" * 5000)
    (home / "media" / "chat" / "photo.jpg").write_bytes(b"jpg")
    (home / "voice_cache" / "a.wav").write_bytes(b"wav")
    (home / "core.log").write_text("log", encoding="utf-8")
    (home / "core.pid").write_text("123", encoding="utf-8")
    (home / "core.lock").write_text("", encoding="utf-8")
    (home / "config.json").write_text(json.dumps({"vault": "x"}), encoding="utf-8")
    (home / "core.token").write_text("SECRET-TOKEN", encoding="utf-8")
    (home / "lessons" / "l1.md").write_text("lesson", encoding="utf-8")
    con = sqlite3.connect(home / "proposals.db")
    con.execute("CREATE TABLE p(id TEXT, v TEXT)")
    con.execute("INSERT INTO p VALUES ('a','one')")
    con.commit()
    con.close()
    wal = sqlite3.connect(home / "runtime.db")
    wal.execute("PRAGMA journal_mode=WAL")
    wal.execute("PRAGMA wal_autocheckpoint=0")
    wal.execute("CREATE TABLE t(n INTEGER)")
    wal.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    wal.commit()  # the rows live in runtime.db-wal, not in runtime.db
    return home, wal


def names(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return {n[len(backup.ROOT_IN_ZIP):] for n in z.namelist() if n.startswith(backup.ROOT_IN_ZIP)}


def test_a_backup_holds_state_databases_config_and_lessons_and_leaves_out_the_heavy_and_the_noise(settings, tmp_path):
    _home, wal = make_home(tmp_path)
    settings.backup_dir = str(tmp_path / "backups")
    res = backup.run(settings, now=1_800_000_000)
    wal.close()
    got = names(res["zip"])
    assert {"config.json", "core.token", "lessons/l1.md", "proposals.db", "runtime.db"} <= got
    assert not any(n.startswith(("kokoro/", "media/", "voice_cache/")) for n in got)
    assert not {"core.log", "core.pid", "core.lock"} & got
    assert not any(n.endswith(("-wal", "-shm")) for n in got)
    assert res["databases"] == 2 and res["skipped"] >= 6
    assert Path(res["zip"]).name == "waggle-backup-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(1_800_000_000)) + ".zip"
    assert not list((tmp_path / "backups").glob("*.part"))


def test_a_database_is_copied_with_its_unflushed_wal_rows(settings, tmp_path):
    _home, wal = make_home(tmp_path)
    settings.backup_dir = str(tmp_path / "backups")
    res = backup.run(settings)
    wal.close()
    out = tmp_path / "unpacked"
    backup.restore(res["zip"], to=str(out))
    con = sqlite3.connect(out / "runtime.db")
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 100  # a plain file copy would have lost these
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert (out / "core.token").read_text(encoding="utf-8") == "SECRET-TOKEN"
    assert not (out / "runtime.db-wal").exists()


def test_the_default_folder_is_waggle_backups_in_the_profile_and_a_setting_changes_it(settings, tmp_path):
    assert Settings().backup_dir == "" and Settings().backup_keep == 14
    assert backup.backup_dir(Settings()) == Path.home() / "WaggleBackups"
    settings.backup_dir = str(tmp_path / "elsewhere")
    assert backup.backup_dir(settings) == tmp_path / "elsewhere"


@pytest.mark.parametrize("folder", ["OneDrive", "OneDrive - Some School", "Dropbox", "Google Drive", "iCloudDrive"])
def test_a_cloud_folder_is_refused_because_the_data_holds_tokens(settings, tmp_path, folder):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / folder / "WaggleBackups")
    with pytest.raises(backup.BackupError, match="cloud folder"):
        backup.run(settings)
    assert not (tmp_path / folder).exists()  # nothing was even created there


def test_the_backup_folder_cannot_be_inside_the_data_home(settings, tmp_path):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(Path(config.HOME) / "backups")
    with pytest.raises(backup.BackupError, match="outside the data home"):
        backup.run(settings)


def test_only_the_newest_fourteen_are_kept_and_only_our_own_zips_are_deleted(settings, tmp_path):
    make_home(tmp_path)[1].close()
    dest = tmp_path / "backups"
    settings.backup_dir = str(dest)
    dest.mkdir()
    (dest / "notes.zip").write_bytes(b"mine")
    (dest / "waggle-backup-old.txt").write_text("mine", encoding="utf-8")
    day = 86400
    t0 = 1_800_000_000
    for i in range(17):
        backup.run(settings, now=t0 + i * day)
    zips = sorted(p.name for p in dest.glob("waggle-backup-*.zip"))
    assert len(zips) == 14
    assert zips[0].startswith(time.strftime("waggle-backup-%Y%m%d", time.localtime(t0 + 3 * day)))  # days 0-2 went
    assert (dest / "notes.zip").exists() and (dest / "waggle-backup-old.txt").exists()
    settings.backup_keep = 3
    res = backup.run(settings, now=t0 + 20 * day)
    assert len(list(dest.glob("waggle-backup-*.zip"))) == 3 and len(res["removed"]) == 12


def test_a_failed_backup_leaves_no_part_file_and_deletes_no_old_backup(settings, tmp_path, monkeypatch):
    make_home(tmp_path)[1].close()
    dest = tmp_path / "backups"
    settings.backup_dir = str(dest)
    backup.run(settings, now=1_800_000_000)

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(backup, "copy_sqlite", boom)
    with pytest.raises(KeyboardInterrupt):
        backup.run(settings, now=1_800_000_100)
    assert len(list(dest.glob("*.zip"))) == 1 and not list(dest.glob("*.part"))


def test_one_unreadable_file_does_not_ruin_the_night(settings, tmp_path, monkeypatch):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / "backups")
    real = backup.is_sqlite

    def flaky(path):
        if Path(path).name == "proposals.db":
            raise OSError("locked")
        return real(path)
    monkeypatch.setattr(backup, "is_sqlite", flaky)
    res = backup.run(settings)
    assert res["problems"] and "proposals.db" in res["problems"][0] and "config.json" in names(res["zip"])


def test_list_shows_newest_first_with_counts_from_the_manifest(settings, tmp_path):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / "backups")
    assert backup.backups(settings) == []
    backup.run(settings, now=1_800_000_000)
    backup.run(settings, now=1_800_086_400)
    rows = backup.backups(settings)
    assert [r["created"] for r in rows] == [1_800_086_400, 1_800_000_000] and rows[0]["databases"] == 2
    assert backup.latest_age_h(settings, now=1_800_086_400 + 7200) == pytest.approx(2.0)


def test_restore_dry_run_checks_the_zip_lists_the_files_and_writes_nothing(settings, tmp_path):
    home, wal = make_home(tmp_path)
    settings.backup_dir = str(tmp_path / "backups")
    res = backup.run(settings)
    wal.close()
    before = sorted(p.name for p in home.rglob("*") if p.is_file())
    report = backup.restore(res["zip"], dry_run=True)
    assert report["dry_run"] and report["ok"] and report["files"] >= 5 and report["would_overwrite"] >= 5
    assert report["target"] == str(home)
    assert sorted(p.name for p in home.rglob("*") if p.is_file()) == before  # nothing written
    out = backup.restore(res["zip"], to=str(tmp_path / "x"), dry_run=True)
    assert out["would_overwrite"] == 0 and not (tmp_path / "x").exists()


def test_restore_dry_run_catches_a_corrupt_database_and_a_damaged_zip(settings, tmp_path):
    make_home(tmp_path)[1].close()
    dest = tmp_path / "backups"
    settings.backup_dir = str(dest)
    good = Path(backup.run(settings, now=1_800_000_000)["zip"])
    bad_db = dest / "waggle-backup-20300101-000000.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad_db, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, b"SQLite format 3\x00" + b"\xff" * 300 if n.endswith("proposals.db") else zin.read(n))
    report = backup.restore(bad_db, dry_run=True)
    assert report["ok"] is False and any("proposals.db" in p for p in report["problems"])
    junk = dest / "waggle-backup-20310101-000000.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(backup.BackupError, match="not a valid zip"):
        backup.restore(junk, dry_run=True)
    with pytest.raises(backup.BackupError, match="no such backup"):
        backup.restore(dest / "missing.zip", dry_run=True)


def test_a_real_restore_needs_a_place_and_an_in_place_one_needs_yes_and_saves_the_present_state_first(settings, tmp_path):
    home, wal = make_home(tmp_path)
    settings.backup_dir = str(tmp_path / "backups")
    res = backup.run(settings)
    wal.close()
    with pytest.raises(backup.BackupError, match="say where"):
        backup.restore(res["zip"])
    with pytest.raises(backup.BackupError, match="--yes"):
        backup.restore(res["zip"], in_place=True, settings=settings)
    (home / "config.json").write_text(json.dumps({"vault": "CHANGED"}), encoding="utf-8")
    out = backup.restore(res["zip"], in_place=True, yes=True, settings=settings)
    assert json.loads((home / "config.json").read_text(encoding="utf-8")) == {"vault": "x"}
    saved = Path(out["backup_of_now"])
    assert saved.exists() and saved.name.startswith(backup.PRE_RESTORE_PREFIX)
    with zipfile.ZipFile(saved) as z:
        assert json.loads(z.read("waggle/config.json").decode()) == {"vault": "CHANGED"}


def test_restore_refuses_a_zip_that_would_write_outside_its_folder(settings, tmp_path):
    evil = tmp_path / "waggle-backup-20300101-000000.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("manifest.json", json.dumps({"files": [], "databases": []}))
        z.writestr("waggle/../../escape.txt", "x")
    with pytest.raises(backup.BackupError, match="unsafe path"):
        backup.restore(evil, to=str(tmp_path / "out"))
    assert not (tmp_path / "escape.txt").exists()


def test_the_nightly_job_and_timer_are_registered_at_0330(settings):
    from waggle.core import jobs, plugins, timers
    assert "waggle.backup" in plugins.MODULES
    jobs._handlers.clear()
    try:
        assert backup.register_core() == {"jobs": ["backup.nightly"], "timers": ["backup.nightly"]}
        t = timers.get("backup.nightly")
        assert t["spec"] == "30 3 * * *" and t["job_kind"] == "backup.nightly" and t["catch_up"] == "latest"
        assert jobs.handler("backup.nightly") is not None
    finally:
        jobs._handlers.clear()


def test_the_nightly_job_backs_up_and_reports_a_summary(settings, tmp_path):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / "backups")
    out = backup.nightly_job({}, settings=settings)
    assert out["files"] >= 5 and out["databases"] == 2 and Path(out["zip"]).exists()


def test_the_doctor_warns_without_a_backup_or_with_an_old_one(settings, tmp_path):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / "backups")
    assert backup.doctor_check(settings)[0] == "warn"
    backup.run(settings, now=1_800_000_000)
    assert backup.doctor_check(settings, now=1_800_000_000 + 10 * H)[0] == "ok"
    st, detail, _ = backup.doctor_check(settings, now=1_800_000_000 + 40 * H)
    assert st == "warn" and "old" in detail


def test_the_command_line_runs_lists_and_dry_runs(settings, tmp_path, capsys):
    make_home(tmp_path)[1].close()
    settings.backup_dir = str(tmp_path / "backups")
    settings.save()
    assert backup.main(["list"]) == 0 and "No backups" in capsys.readouterr().out
    assert backup.main(["run"]) == 0 and "Backed up" in capsys.readouterr().out
    assert backup.main(["list"]) == 0 and "waggle-backup-" in capsys.readouterr().out
    z = next((tmp_path / "backups").glob("*.zip"))
    assert backup.main(["restore", str(z), "--dry-run"]) == 0 and "Backup check: passed" in capsys.readouterr().out
    assert backup.main(["restore", str(z)]) == 1 and "say where" in capsys.readouterr().err


def test_the_backup_module_starts_no_processes():
    src = Path(backup.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in src and "os.system" not in src and "Popen" not in src
