"""Calendar events reach the model in the owner's own time zone (2026-11-25: a UTC start read as a local date
made Tuesday's practice look like Wednesday's)."""
import datetime as dt

from waggle import google


def test_utc_start_becomes_local_time_with_weekday(monkeypatch):
    utc = "2026-12-02T00:00:00Z"
    iso, when = google.local_time(utc)
    local = dt.datetime(2026, 12, 2, tzinfo=dt.timezone.utc).astimezone()
    assert iso == local.isoformat(timespec="minutes")
    assert when.startswith(local.strftime("%a %b")) and f"{local.hour % 12 or 12}:00" in when


def test_all_day_and_odd_values_pass_through():
    assert google.local_time("2026-12-01") == ("2026-12-01", "Tue Dec 1 (all day)")
    assert google.local_time("") == ("", "")
    assert google.local_time("soon") == ("soon", "soon")


def test_calendar_list_uses_local_times(monkeypatch):
    monkeypatch.setattr(google, "_need_ready", lambda s=None: "acct")
    monkeypatch.setattr(google, "_call", lambda *a, **k: {"events": [
        {"id": "e1", "summary": "Robotics practice", "start": {"dateTime": "2026-12-02T00:00:00Z"},
         "end": {"dateTime": "2026-12-02T02:00:00Z"}}]})
    ev = google.calendar_list(7)[0]
    assert ev["start"] == google.local_time("2026-12-02T00:00:00Z")[0] and ev["when"]
    assert not ev["start"].endswith("Z")
