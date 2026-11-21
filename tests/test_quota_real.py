"""The real five-hour reading (rate_limit_event) drives the window: fixed start, calibration, fresh vs stale."""
import json
import time

import pytest

from waggle import agent, budget, claude_cli, jobs, quota
from waggle.config import Settings, data_path

# The line every `claude -p --output-format stream-json` run emits (from the real CLI).
EVENT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 1795116000, "rateLimitType": "five_hour",
    "unifiedWindows": {"five_hour": {"utilization": 0.13, "resetsAt": 1795116000, "status": "allowed"},
                       "seven_day": {"utilization": 0.12, "resetsAt": 1795615200, "status": "allowed"}}}}
H = 3600


def info(util, resets_at):
    return {"status": "allowed", "resetsAt": resets_at, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": resets_at, "status": "allowed"}}}


def transcript(tmp_path, rows):
    """rows: (epoch, units): one Sonnet message each with `units` million input tokens (weight 1.0)."""
    root = tmp_path / "transcripts"
    (root / "p").mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (ts, units) in enumerate(rows):
        lines.append(json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                                 "message": {"id": f"m{i}", "model": "claude-sonnet-5-5",
                                             "usage": {"input_tokens": int(units * 1e6)}}}))
    (root / "p" / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")


def reading():
    return json.loads(data_path("real_usage.json").read_text(encoding="utf-8"))


# ---- parsing --------------------------------------------------------------------------------------------------

def test_parses_the_sample_event(settings):
    row = quota.note_real_usage(EVENT["rate_limit_info"], now=1795100000)
    assert row["utilization"] == 0.13 and row["resets_at"] == 1795116000 and row["seen_at"] == 1795100000
    assert reading()["units_at_seen"] == 0.0


def test_top_level_fallback_only_for_five_hour(settings):
    quota.note_real_usage({"rateLimitType": "five_hour", "utilization": 0.4, "resetsAt": 1795116000}, now=1795100000)
    assert reading()["utilization"] == 0.4
    data_path("real_usage.json").unlink()
    assert quota.note_real_usage({"rateLimitType": "seven_day", "utilization": 0.4, "resetsAt": 1795116000}) is None
    assert not data_path("real_usage.json").exists()


@pytest.mark.parametrize("bad", [None, [], "x", 5, {}, {"unifiedWindows": 5}, {"unifiedWindows": {"five_hour": 3}},
                                 info("x", 1795116000), info(float("nan"), 1795116000), info(-1, 1795116000),
                                 info(0.3, None), info(0.3, "soon"), info(True, 1795116000)])
def test_bad_input_is_ignored_and_never_raises(settings, bad):
    assert quota.note_real_usage(bad) is None
    assert not data_path("real_usage.json").exists()


def test_a_broken_store_never_raises(settings, monkeypatch):
    monkeypatch.setattr(quota, "data_path", lambda name: (_ for _ in ()).throw(OSError("disk")))
    assert quota.note_real_usage(info(0.3, 1795116000)) is None


def test_every_stream_parser_notes_the_event(settings):
    line = json.dumps(EVENT)
    claude_cli.StreamParser().feed(line)
    assert reading()["utilization"] == 0.13
    data_path("real_usage.json").unlink()
    agent.parse_stream([line])
    assert reading()["utilization"] == 0.13
    data_path("real_usage.json").unlink()
    jobs.parse_stream(line + "\n" + json.dumps({"type": "result", "result": "ok"}))
    assert reading()["utilization"] == 0.13


def test_a_failing_note_never_breaks_a_parser(settings, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(quota, "note_real_usage", boom)
    line = json.dumps(EVENT)
    r = claude_cli.StreamParser()
    r.feed(line)
    assert r.rate_limit["status"] == "allowed" and r.garbage == 0
    assert agent.parse_stream([line, json.dumps({"type": "result", "result": "x"})])[0]["result"] == "x"
    assert jobs.parse_stream(line + "\n" + json.dumps({"type": "result", "result": "x"}))[1] == "x"


# ---- the fixed window -----------------------------------------------------------------------------------------

def test_window_start_no_reading_is_rolling(settings):
    assert quota.window_start(10_000_000, 5) == 10_000_000 - 5 * H


def test_window_start_from_a_live_reading(settings):
    now = 10_000_000
    quota.note_real_usage(info(0.1, now + 2 * H), now=now)
    assert quota.window_start(now, 5) == now - 3 * H


def test_live_reading_excludes_the_previous_window(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 4 * H, 3.0), (now - 1 * H, 1.0)])  # 3.0 belongs to the window before the reset
    assert quota.window_usage(5, now) == pytest.approx(4.0)  # no reading: rolling
    quota.note_real_usage(info(0.1, now + 2 * H), now=now)  # window began 3 h ago
    assert quota.window_usage(5, now) == pytest.approx(1.0)


def test_expired_reading_starts_at_the_first_usage_after_the_reset(settings, tmp_path):
    now = time.time()
    quota.note_real_usage(info(0.5, now - 1 * H), now=now - 2 * H)
    transcript(tmp_path, [(now - 90 * 60, 2.0), (now - 40 * 60, 1.0), (now - 10 * 60, 1.0)])
    assert quota.window_start(now, 5) == pytest.approx(now - 40 * 60, abs=1)
    assert quota.window_usage(5, now) == pytest.approx(2.0)


def test_expired_reading_start_is_capped_at_the_window_length(settings, tmp_path):
    now = time.time()
    quota.note_real_usage(info(0.5, now - 6 * H), now=now - 7 * H)
    transcript(tmp_path, [(now - 5.5 * H, 9.0), (now - 4 * H, 1.0), (now - 1 * H, 1.0)])
    assert quota.window_start(now, 5) == pytest.approx(now - 4 * H, abs=1)
    assert quota.window_usage(5, now) == pytest.approx(2.0)


def test_expired_reading_with_no_usage_since_is_an_empty_window(settings, tmp_path):
    now = time.time()
    quota.note_real_usage(info(0.5, now - 1 * H), now=now - 2 * H)
    transcript(tmp_path, [(now - 3 * H, 2.0)])
    assert quota.window_usage(5, now) == 0.0


# ---- calibration ----------------------------------------------------------------------------------------------

def test_calibration_sets_the_cap_and_calibrated_at(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 1 * H, 2.0)])
    assert not quota.calibrated()
    quota.note_real_usage(info(0.4, now + 3 * H), now=now)
    assert reading()["units_at_seen"] == pytest.approx(2.0)
    assert quota.window_estimate(Settings.load()) == pytest.approx(5.0)
    assert quota.calibrated()
    assert quota._learned("calibrated_at") == pytest.approx(now)


def test_calibration_is_smoothed_and_skips_low_readings(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 1 * H, 2.0)])
    quota.note_real_usage(info(0.4, now + 3 * H), now=now)       # 5.0
    quota.note_real_usage(info(0.2, now + 3 * H), now=now + 60)  # 10.0
    assert quota.window_estimate(Settings.load()) == pytest.approx(7.5)
    quota.note_real_usage(info(0.1, now + 3 * H), now=now + 120)  # below 0.15: not used
    assert quota.window_estimate(Settings.load()) == pytest.approx(7.5)
    assert len(reading()["caps"]) == 2


def test_calibration_stays_within_the_bounds(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 1 * H, 500.0)])
    quota.note_real_usage(info(0.5, now + 3 * H), now=now)  # 1000 units
    s = Settings.load()
    assert quota.window_estimate(s) == quota.estimate_bounds(s)[1]


def test_no_calibration_without_counted_units(settings):
    quota.note_real_usage(info(0.5, time.time() + 3 * H))
    assert not quota.calibrated()
    assert quota.window_estimate(Settings.load()) == 6.0


# ---- fresh vs stale, status, decisions ------------------------------------------------------------------------

def _two_events(tmp_path, now, seen_ago):
    """1.0 unit before the reading, 1.0 after it; the window began 3 h ago. util 0.1 is below the calibration line,
    so the cap stays at the default 6.0 and the real and estimated numbers differ."""
    transcript(tmp_path, [(now - seen_ago - 600, 1.0), (now - 120, 1.0)])
    quota.note_real_usage(info(0.1, now + 2 * H), now=now - seen_ago)


def test_fresh_reading_is_the_used_fraction(settings, tmp_path):
    now = time.time()
    _two_events(tmp_path, now, seen_ago=300)
    st = quota.status(settings)
    assert st["source"] == "real" and st["resets_at"] == pytest.approx(now + 2 * H)
    assert st["window_pct"] == pytest.approx(100 * (0.1 + 1.0 / 6), abs=0.1)  # reading + what was spent since
    assert st["window_used_units"] == pytest.approx(st["window_pct"] / 100 * 6.0, abs=0.01)


def test_stale_reading_falls_back_to_the_estimate(settings, tmp_path):
    now = time.time()
    _two_events(tmp_path, now, seen_ago=40 * 60)
    st = quota.status(settings)
    assert st["source"] == "estimate" and st["resets_at"] == pytest.approx(now + 2 * H)
    assert st["window_pct"] == pytest.approx(100 * 2.0 / 6, abs=0.1)  # fixed window: both events count


def test_reading_from_an_old_window_is_not_fresh(settings, tmp_path):
    now = time.time()
    quota.note_real_usage(info(0.9, now - 60), now=now - 120)  # window already reset
    st = quota.status(settings)
    assert st["source"] == "estimate" and st["resets_at"] is None and st["window_pct"] == 0.0


def test_status_without_any_reading_is_the_old_estimate(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 4 * H, 3.0)])
    st = quota.status(settings)
    assert st["source"] == "estimate" and st["resets_at"] is None
    assert st["window_pct"] == pytest.approx(50.0)


def test_real_54_percent_passes_the_background_line_though_the_rolling_sum_is_high(settings, tmp_path):
    now = time.time()
    # 8.0 units belong to the window before the reset; this window used 5.4 and the CLI says that is 54% (cap 10).
    transcript(tmp_path, [(now - 4 * H, 8.0), (now - 1 * H, 5.4)])
    rolling = quota.window_usage(5, now)
    quota.note_real_usage(info(0.54, now + 2 * H), now=now)
    cap = quota.window_estimate(Settings.load())
    assert rolling / cap > 1.0  # the rolling sum alone would call this window over its limit
    st = quota.status(settings)
    assert st["source"] == "real" and st["window_pct"] == pytest.approx(54.0, abs=0.5)
    ok, why, _ = budget.check("sonnet", "objective", Settings.load(), now)
    assert ok, why
    assert quota.can_escalate("sonnet", Settings.load(), now)[0]
    with budget.background("objective", user_started=True):
        ok, why = quota.can_escalate("sonnet", Settings.load(), now)
    assert ok, why


def test_background_work_is_refused_above_60_on_the_real_number(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 1 * H, 1.0)])
    quota.note_real_usage(info(0.7, now + 2 * H), now=now)
    ok, why, _ = budget.check("sonnet", "objective", Settings.load(), now)
    assert not ok and "window at 70%" in why
    assert quota.can_escalate("sonnet", Settings.load(), now)[0]  # chat may go to stop_at (85%)
    quota.note_real_usage(info(0.9, now + 2 * H), now=now)
    ok, why = quota.can_escalate("sonnet", Settings.load(), now)
    assert not ok and "90%" in why


def test_window_used_units_is_pct_times_cap(settings, tmp_path):
    now = time.time()
    transcript(tmp_path, [(now - 1 * H, 2.0)])
    quota.note_real_usage(info(0.4, now + 3 * H), now=now)  # cap 5.0
    s = Settings.load()
    assert quota.window_used_units(s, now) == pytest.approx(2.0)  # 0.4 * 5.0
    assert quota.window_view(s, now)["pct"] == pytest.approx(0.4)
