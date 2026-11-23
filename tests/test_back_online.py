"""The owner's "I'm back" message after an outage (owner decision 2026-11-21)."""
from waggle.core import runtime

NOW = 1795463235.0


def _report(gap_s, skipped=(), fired=0):
    return {"now": NOW, "last_tick": NOW - gap_s, "fired": [{}] * fired,
            "skipped": [{"timer_id": t, "count": 1} for t in skipped], "late": []}


def test_short_restarts_say_nothing():
    assert runtime.back_online_text(_report(40)) is None  # a deploy or watchdog restart
    assert runtime.back_online_text({"now": NOW, "last_tick": None, "skipped": [], "fired": []}) is None  # first start


def test_an_outage_names_the_gap_and_what_was_skipped():
    text = runtime.back_online_text(_report(20 * 3600, skipped=["outreach.brief", "outreach.evening", "dropbox.scan"],
                                            fired=15))
    assert text.startswith("Back online at ")
    assert "(20 h)" in text
    assert "the evening note" in text and "the morning brief" in text
    assert "dropbox" not in text  # housekeeping timers are not news
    assert "15 scheduled job(s)" in text


def test_the_notice_never_breaks_start_up(monkeypatch):
    from waggle import notify
    monkeypatch.setattr(notify, "deliver", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert runtime._back_online_notice(_report(3600)) is None


def test_the_notice_is_queued_once_per_outage(monkeypatch):
    from waggle import notify
    calls = []
    monkeypatch.setattr(notify, "deliver", lambda kind, text, **k: calls.append((kind, k["ref"], k["origin"])) or {})
    runtime._back_online_notice(_report(3600))
    assert calls == [("text", f"back-online:{int(NOW - 3600)}", "core")]
