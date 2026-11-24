"""The world calendar (waggle/worldcal.py): parsing and caching. The network is always a fake."""
import datetime as dt

from waggle import worldcal

WIKITEXT = """==Holidays and observances==
* Christian [[feast day]]:
**Blessed [[Somebody]]<ref name="x">{{cite book |title=T |page=1}}</ref>
** [[Another One]]
* [[Day of Discovery of Puerto Rico]] ([[Puerto Rico]])
* [[International Men's Day]]<ref>{{cite book |last1=A |title=B}}</ref>
* [[Martyrs' Day (India)#19 November|Martyrs' Day]] ([[Uttar Pradesh]], India)
* [[World Toilet Day]]
"""
DAY = dt.date(2026, 11, 19)


def fake(calls, text=WIKITEXT):
    def get(params):
        calls.append(params)
        if params["prop"] == "sections":
            return {"parse": {"sections": [{"index": "1", "line": "Events"},
                                           {"index": "13", "line": "Holidays and observances"}]}}
        return {"parse": {"wikitext": text}}
    return get


def test_parse_keeps_top_level_entries_as_plain_text():
    assert worldcal.parse(WIKITEXT) == ["Day of Discovery of Puerto Rico (Puerto Rico)", "International Men's Day",
                                        "Martyrs' Day (Uttar Pradesh, India)", "World Toilet Day"]


def test_page_name_and_the_request_name_only_a_date(settings):
    calls = []
    worldcal.observances(DAY, get=fake(calls), now=1000.0)
    assert worldcal.page_name(DAY) == "November_19"
    assert [c["page"] for c in calls] == ["November_19", "November_19"] and calls[1]["section"] == "13"


def test_a_date_is_fetched_once_and_then_read_from_the_cache(settings):
    calls = []
    first = worldcal.observances(DAY, get=fake(calls), now=1000.0)
    again = worldcal.observances(DAY, get=fake(calls), now=1000.0 + 86400)
    other_year = worldcal.observances(dt.date(2027, 11, 19), get=fake(calls), now=2000.0)
    assert "International Men's Day" in first and first == again == other_year and len(calls) == 2


def test_a_failed_fetch_gives_nothing_and_waits_before_trying_again(settings):
    tries = []

    def down(params):
        tries.append(1)
        raise OSError("offline")
    assert worldcal.observances(DAY, get=down, now=1000.0) == []
    assert worldcal.observances(DAY, get=down, now=1000.0 + 60) == [] and len(tries) == 1
    calls = []
    assert worldcal.observances(DAY, get=fake(calls), now=1000.0 + worldcal.RETRY_S + 1)


def test_an_old_list_is_refreshed_and_kept_when_the_refresh_fails(settings):
    calls = []
    worldcal.observances(DAY, get=fake(calls), now=1.0)
    later = (worldcal.REFRESH_DAYS + 2) * 86400.0

    def down(params):
        raise OSError("offline")
    assert "World Toilet Day" in worldcal.observances(DAY, get=down, now=later)


def test_lines_cover_today_and_the_days_after(settings):
    now = dt.datetime(2026, 11, 18, 16, 15).timestamp()
    out = worldcal.lines(now, days=2, get=fake([]))
    assert out[0].startswith("Wed 2026-11-18: ") and out[1].startswith("Thu 2026-11-19: ") and "Men's Day" in out[1]


def test_without_a_fake_the_test_suite_never_reaches_wikipedia(settings):
    assert worldcal.observances(DAY, now=1000.0) == []
