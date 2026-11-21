"""Focus blocks with ActivityWatch, the local model and the two channels mocked: the owner starts one, drifting twice
in a row nudges (at most every 3 minutes), the end gives one summary line, and no window text is ever kept."""
import datetime as dt
import json

import pytest

from waggle import aw_sensor, engine_commands, focus
from waggle.config import data_path
from waggle.core import jobs, timers

NOW = 1_790_000_000.0


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


class Screen:
    """ActivityWatch as a screen whose foreground window the test changes. Pass it as get=."""

    def __init__(self, up=True):
        self.up, self.now, self.app, self.title, self.url, self.afk = up, NOW, "code.exe", "x", "", False

    def show(self, now, app, title, url="", afk=False):
        self.now, self.app, self.title, self.url, self.afk = now, app, title, url, afk

    def __call__(self, path, params=None, timeout=4.0):
        if not self.up:
            raise aw_sensor.AwDown("down")
        if path == "/api/0/buckets/":
            return {"w": {"type": "currentwindow", "last_updated": "1"}, "a": {"type": "afkstatus", "last_updated": "1"},
                    "web": {"type": "web.tab.current", "last_updated": "1"}}
        ts = iso(self.now - 5)
        if path.endswith("/w/events"):
            return [{"timestamp": ts, "duration": 5, "data": {"app": self.app, "title": self.title}}]
        if path.endswith("/a/events"):
            return [{"timestamp": ts, "duration": 5, "data": {"status": "afk" if self.afk else "not-afk"}}]
        if path.endswith("/web/events"):
            return [{"timestamp": ts, "duration": 5, "data": {"url": self.url, "title": "t"}}] if self.url else []
        raise aw_sensor.AwDown("404")


class Channels:
    """The two ways out, recorded."""

    def __init__(self, at_pc=True):
        self.said, self.texted, self.at_pc = [], [], at_pc

    def seams(self):
        return {"at_pc": self.at_pc, "voice_ready": True, "hand_over": lambda i, t, n: self.said.append(t),
                "send": self.texted.append}


def begin(settings, screen, topic="calculus", minutes=45):
    out = focus.start(topic, minutes, settings, now=NOW, get=screen)
    assert out["ok"], out
    return out


def run_checks(settings, screen, ch, steps, local=None):
    """steps: [(seconds after start, app, title, url)] -> the reports."""
    reports = []
    for sec, app, title, *rest in steps:
        screen.show(NOW + sec, app, title, *(rest or [""]))
        reports.append(focus.tick(settings, NOW + sec, screen, local, **ch.seams()))
    return reports


def test_parse_request_reads_minutes_and_topic():
    p = focus.parse_request
    assert p("45 calculus") == (45, "calculus") and p("calculus for 45 minutes") == (45, "calculus")
    assert p("for an hour on physics") == (60, "physics") and p("1.5 hours ftc robot") == (90, "ftc robot")
    assert p("half an hour of reading") == (30, "reading") and p("45m calculus") == (45, "calculus")
    assert p("math 1402 for 40 min") == (40, "math 1402")           # the course number is not the length
    assert p("calculus") == (30, "calculus") and p("") == (30, "") and p("9999 x")[0] == 30
    assert p("500 minutes of work")[0] == 240                       # capped


def test_start_needs_activitywatch_and_arms_the_timers(settings):
    refused = focus.start("calculus", 45, settings, now=NOW, get=Screen(up=False))
    assert not refused["ok"] and "ActivityWatch" in refused["message"] and focus.load() is None
    out = begin(settings, Screen())
    assert "45 minutes on calculus" in out["message"]
    assert timers.get("focus.check")["spec"] == "90s" and timers.get("focus.end")["kind"] == "once"
    b = focus.load()
    assert b["tokens"] == ["calculus"] and b["project"] == "school" and b["ends"] == NOW + 2700
    again = focus.start("physics", 20, settings, now=NOW + 5, get=Screen())
    assert "Replaced the earlier block (calculus)" in again["message"]


def test_two_drifts_in_a_row_nudge_then_at_most_every_three_minutes(settings):
    screen, ch = Screen(), Channels(at_pc=True)
    begin(settings, screen)
    yt = ("chrome.exe", "Funny cats - YouTube", "https://www.youtube.com/watch?v=1")
    r = run_checks(settings, screen, ch, [(90, *yt), (180, *yt), (270, *yt), (360, *yt)])
    assert [x["verdict"] for x in r] == ["off"] * 4
    assert [x["nudged"] for x in r] == [False, "voice", False, "voice"]     # check 2 nudges; 270 is only 90 s later
    assert "sir" in ch.said[0].lower() and not ch.texted
    assert len(ch.said) == 2 and ch.said[0] != ch.said[1]
    assert focus.load()["drifts"] == 1                                       # one episode, two nudges


def test_a_nudge_goes_to_telegram_when_the_owner_is_away_from_the_pc(settings):
    screen, ch = Screen(), Channels(at_pc=False)
    begin(settings, screen)
    game = ("minecraft.exe", "Minecraft 1.21")
    r = run_checks(settings, screen, ch, [(90, *game), (180, *game)])
    assert r[1]["nudged"] == "telegram" and ch.said == [] and len(ch.texted) == 1
    assert len(ch.texted[0].split()) <= 8


def test_one_off_check_then_back_to_work_is_not_a_drift(settings):
    screen, ch = Screen(), Channels()
    begin(settings, screen)
    steps = [(90, "chrome.exe", "Funny cats - YouTube"), (180, "chrome.exe", "Calculus homework - Google Docs"),
             (270, "chrome.exe", "Funny cats - YouTube"), (360, "chrome.exe", "Khan Academy - derivatives")]
    r = run_checks(settings, screen, ch, steps)
    assert [x["verdict"] for x in r] == ["off", "on", "off", "on"] and not any(x["nudged"] for x in r)
    assert focus.load()["drifts"] == 0 and ch.said == [] and ch.texted == []


def test_a_title_that_names_the_topic_beats_a_leisure_site(settings):
    screen, ch = Screen(), Channels()
    begin(settings, screen)
    r = run_checks(settings, screen, ch, [(90, "chrome.exe", "Calculus 1 full course - YouTube", "https://youtube.com/w")])
    assert r[0]["verdict"] == "on"


def test_unclear_windows_ask_the_local_model_once_and_unsure_or_failure_is_on_task(settings):
    screen, ch, asked = Screen(), Channels(), []

    def local(topic, win, s):
        asked.append((topic, win["app"]))
        return {"Gmail": "unrelated", "Notes": "unsure"}[win["title"].split()[0]]
    begin(settings, screen)
    steps = [(90, "chrome.exe", "Gmail inbox"), (180, "chrome.exe", "Gmail inbox"), (270, "chrome.exe", "Notes app")]
    r = run_checks(settings, screen, ch, steps, local)
    assert [x["verdict"] for x in r] == ["off", "off", "on"] and r[1]["nudged"] == "voice"
    assert asked == [("calculus", "chrome"), ("calculus", "chrome")]          # Gmail once (cached), Notes once

    def broken(topic, win, s):
        raise TimeoutError("2 s")
    screen.show(NOW + 360, "chrome.exe", "Something new")
    assert focus.tick(settings, NOW + 360, screen, broken, **ch.seams())["verdict"] == "on"
    assert focus._backoff["until"] > NOW + 360                                 # then the model is left alone a while
    screen.show(NOW + 450, "chrome.exe", "Another new thing")
    assert focus.tick(settings, NOW + 450, screen, lambda *a: pytest.fail("asked during backoff"), **ch.seams())["verdict"] == "on"


def test_the_local_call_uses_the_plain_ollama_chat_with_a_two_second_cap(settings, monkeypatch):
    seen = {}

    def fake_chat_json(messages, schema=None, **kw):
        seen.update(kw, schema=schema, prompt=messages[0]["content"])
        return {"verdict": "unrelated"}
    from waggle import ollama
    monkeypatch.setattr(ollama, "chat_json", fake_chat_json)
    win = {"app": "chrome", "title": "Some page", "host": "", "afk": False}
    assert focus.ask_local("calculus", win, settings) == "unrelated"
    assert seen["timeout"] == 2.0 and seen["schema"] == focus.SCHEMA and "num_ctx" not in seen and "options" not in seen
    assert seen["model"] == settings.local_model and "calculus" in seen["prompt"]


def test_afk_and_a_blind_screen_never_count_as_drift(settings):
    screen, ch = Screen(), Channels()
    begin(settings, screen)
    r = run_checks(settings, screen, ch, [(90, "chrome.exe", "Funny cats - YouTube"), (180, "x.exe", "y", "", True)])
    assert r[1]["verdict"] == "away" and focus.load()["streak"] == 0
    screen.up = False
    assert focus.tick(settings, NOW + 270, screen, None, **ch.seams())["verdict"] == "blind"
    assert focus.load()["blind_s"] > 0 and ch.said == [] and ch.texted == []


def test_the_block_ends_with_one_summary_line_and_removes_its_timers(settings):
    screen, ch = Screen(), Channels(at_pc=False)
    begin(settings, screen, "calculus", 5)
    steps = [(90, "chrome.exe", "Calculus homework - Google Docs"), (180, "chrome.exe", "Funny cats - YouTube"),
             (270, "chrome.exe", "Funny cats - YouTube")]
    run_checks(settings, screen, ch, steps)
    out = focus.tick(settings, NOW + 300, screen, None, **ch.seams())          # the end time
    assert out["finished"] and out["via"] == "telegram"
    assert ch.texted[-1] == out["line"] and out["line"].startswith("Focus block done, sir: 2 of 5 minutes on calculus")
    assert out["line"].endswith("1 drift.")
    assert focus.load() is None and timers.get("focus.check") is None and timers.get("focus.end") is None
    assert focus.tick(settings, NOW + 400, screen, None, **ch.seams()) == {"idle": True}   # a second finisher does nothing
    assert len([t for t in ch.texted if t.startswith("Focus block")]) == 1


def test_stop_ends_early_and_says_so(settings):
    screen, ch = Screen(), Channels(at_pc=True)
    begin(settings, screen)
    run_checks(settings, screen, ch, [(90, "chrome.exe", "Calculus homework - Google Docs")])
    out = focus.stop(settings, NOW + 120, **ch.seams())
    assert out["ok"] and out["message"].startswith("Focus block ended early, sir: 2 of 2 minutes on calculus, no drifting")
    assert ch.said == [out["message"]] and focus.stop(settings)["ok"] is False


def test_nothing_about_the_windows_is_stored_or_logged(settings):
    screen, ch = Screen(), Channels()
    begin(settings, screen, "calculus", 10)
    steps = [(90, "chrome.exe", "PRIVATE-TITLE-77 - YouTube", "https://www.youtube.com/watch?v=PRIVURL55"),
             (180, "chrome.exe", "PRIVATE-TITLE-77 - YouTube", "https://www.youtube.com/watch?v=PRIVURL55"),
             (270, "chrome.exe", "SECRET-MAIL-SUBJECT Gmail", "https://mail.google.com/PRIVURL56")]
    run_checks(settings, screen, ch, steps, lambda t, w, s: "unrelated")
    focus.finish(settings, NOW + 400, "stopped", **ch.seams())
    blob = b"".join(p.read_bytes() for p in data_path("").iterdir() if p.is_file())
    for secret in (b"PRIVATE-TITLE", b"PRIVURL", b"SECRET-MAIL", b"mail.google.com"):
        assert secret not in blob, secret
    assert b"PRIVATE-TITLE" not in json.dumps(ch.texted + ch.said).encode()


def test_the_core_registers_the_job_and_rearms_after_a_restart(settings):
    out = focus.register_core()
    assert out == {"jobs": ["focus.check"], "timers": []} and jobs.handler("focus.check") is not None
    assert timers.get("focus.check") is None                                    # no block: no timers, no cost
    begin(settings, Screen())
    timers.remove("focus.check")
    timers.remove("focus.end")
    assert focus.register_core()["timers"] == ["focus.check", "focus.end"] and timers.get("focus.end")
    focus.claim()                                                               # a block that vanished: timers go too
    assert focus.register_core()["timers"] == [] and timers.get("focus.check") is None


def test_the_check_job_does_nothing_when_waggle_is_switched_off(settings, monkeypatch):
    from waggle.core import offswitch
    monkeypatch.setattr(offswitch, "is_off", lambda: True)
    assert focus.check_job({}, settings=settings) == {"skipped": "Waggle is switched off"}


def test_chat_tools_and_the_slash_command(settings, monkeypatch):
    from waggle import policy, waggle_mcp
    for name in ("focus_start", "focus_stop", "focus_status"):
        assert name in waggle_mcp.TOOLS and policy.WAGGLE_TOOLS[name][0] == policy.ALLOW
    monkeypatch.setattr(aw_sensor, "_get", Screen())
    out = json.loads(waggle_mcp.TOOLS["focus_start"][1]({"topic": "calculus", "minutes": 45}))
    assert out["ok"] and out["block"]["minutes"] == 45
    assert json.loads(waggle_mcp.TOOLS["focus_status"][1]({}))["active"] is True
    assert "calculus" in engine_commands.serve("/waggle focus", settings)
    stopped = engine_commands.serve("/waggle focus stop", settings)
    assert stopped.startswith("Focus block ended early") and focus.load() is None
    assert "No focus block" in engine_commands.serve("/waggle focus", settings)
    started = engine_commands.serve("/waggle focus 20 physics", settings)
    assert "20 minutes on physics" in started
    assert "/waggle focus" in engine_commands.HELP


def test_focus_makes_no_claude_call():
    import inspect
    src = inspect.getsource(focus)
    imports = [ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    assert not any(x in ln for ln in imports for x in ("claude_cli", "chat_engine", "cascade", "budget", "engines"))
