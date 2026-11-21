"""The dead-man switch client (waggle/deadman.py). HTTP is a fake: nothing here reaches a real Worker."""
import datetime as dt
import io
import json
import sys
import urllib.error

import pytest

from waggle import deadman, followups, tick, wincred
from waggle.config import Settings
from waggle.core import jobs, outbox, store, timers

URL = "https://waggle-deadman.example.workers.dev"
NOW = dt.datetime(2026, 11, 20, 12, 0).timestamp()


def at(hours):
    return NOW + hours * 3600


class FakeNet:
    """Stands in for urllib's urlopen: records requests, answers from `answers` (path -> dict or an exception)."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, req, timeout=None):
        path = req.full_url[len(URL):]
        body = json.loads(req.data) if req.data else None
        self.calls.append({"method": req.get_method(), "path": path, "body": body, "auth": req.get_header("Authorization"),
                           "timeout": timeout})
        ans = self.answers.get(path.split("?")[0], {"ok": True})
        if isinstance(ans, Exception):
            raise ans
        return _Resp(json.dumps(ans).encode())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(settings):
    settings.deadman_url = URL
    settings.save()
    wincred.write(deadman.CRED, "waggle", "x" * 43)
    return Settings.load()


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(deadman.urllib.request, "urlopen", fake)
    return fake


def reminder(hours, text="charge the batteries", **kw):
    tick.register_jobs()
    return followups.add(at(hours), text, now=NOW, **kw)


# ---- inert until configured -------------------------------------------------------------------------------------

def test_nothing_happens_without_a_url(settings, net):
    wincred.write(deadman.CRED, "waggle", "x" * 43)
    reminder(1)
    assert deadman.beat(NOW) == {"skipped": "not configured"}
    assert deadman.ack("a") is False and deadman.worker_sent("a") is False
    assert net.calls == []


def test_nothing_happens_without_a_token(settings, net):
    settings.deadman_url = URL
    assert deadman.configured(settings) is False
    assert deadman.beat(NOW, settings) == {"skipped": "not configured"}
    assert net.calls == []


def test_only_https_or_localhost_counts(settings):
    for bad in ("http://example.com", "ftp://x", "example.com"):
        settings.deadman_url = bad
        assert deadman.base_url(settings) == ""
    settings.deadman_url = "http://127.0.0.1:8787/"
    assert deadman.base_url(settings) == "http://127.0.0.1:8787"


# ---- what goes out ----------------------------------------------------------------------------------------------

def test_beat_sends_the_next_48_hours_only(cfg, net):
    soon = reminder(2, "call mom")
    reminder(60, "too far ahead")
    reminder(-30, "far too late")
    late = reminder(-2, "still open, overdue")
    reminder(3, "agent made", origin="agent")
    out = deadman.beat(NOW, cfg)
    assert out["ok"] and out["reminders"] == 2
    call = net.calls[0]
    assert (call["method"], call["path"], call["auth"]) == ("POST", "/beat", "Bearer " + "x" * 43)
    ids = [r["id"] for r in call["body"]["reminders"]]
    assert ids == [late, soon]
    assert set(call["body"]["reminders"][0]) == {"id", "at", "text"}
    assert call["body"]["reminders"][1] == {"id": soon, "at": at(2), "text": "call mom"}
    assert set(call["body"]) == {"reminders"}


def test_hide_text_sends_a_generic_line(cfg, net):
    cfg.deadman_hide_text = True
    reminder(1, "dentist about my private thing")
    deadman.beat(NOW, cfg)
    assert net.calls[0]["body"]["reminders"][0]["text"] == "Reminder due"


def test_text_is_cut_to_200(cfg, net):
    reminder(1, "x" * 400)
    deadman.beat(NOW, cfg)
    assert len(net.calls[0]["body"]["reminders"][0]["text"]) == 200


def test_a_delivered_reminder_is_not_sent_again(cfg, net):
    fid = reminder(1)
    followups.mark(fid, "sent", NOW)
    deadman.beat(NOW, cfg)
    assert net.calls[0]["body"]["reminders"] == []


# ---- what the Worker tells back ---------------------------------------------------------------------------------

def test_reminders_the_worker_sent_are_marked_and_their_timers_dropped(cfg, net):
    fid = reminder(-1)
    other = reminder(5)
    assert timers.get(f"reminder:{fid}")
    net.answers["/beat"] = {"ok": True, "sent": [fid, "unknown-id"]}
    out = deadman.beat(NOW, cfg)
    assert out["worker_sent"] == [fid]
    assert followups.get(fid)["status"] == "sent"
    assert "backup service" in followups.get(fid)["reason"]
    assert timers.get(f"reminder:{fid}") is None
    assert followups.get(other)["status"] == "open" and timers.get(f"reminder:{other}")
    assert deadman.status(cfg, NOW, urlopen=net)["worker_sent_total"] == 1


def test_a_failed_beat_is_recorded_and_never_raises(cfg, net):
    net.answers["/beat"] = urllib.error.URLError("offline")
    out = deadman.beat(NOW, cfg)
    assert "could not reach the Worker" in out["error"]
    assert "URLError" in out["error"]
    assert "x" * 43 not in json.dumps(out)
    assert "could not reach" in store.get_meta(deadman.META_ERR)
    net.answers["/beat"] = urllib.error.HTTPError(URL, 401, "no", {}, None)
    assert "HTTP 401" in deadman.beat(NOW, cfg)["error"]
    net.answers["/beat"] = {"ok": True}
    assert deadman.beat(NOW + 300, cfg)["ok"]
    assert store.get_meta(deadman.META_ERR) == ""


def test_ack_and_worker_sent(cfg, net):
    assert deadman.ack("abc", cfg) is True
    assert net.calls[0]["body"] == {"id": "abc"} and net.calls[0]["timeout"] == 4
    net.answers["/sent"] = {"sent": True}
    assert deadman.worker_sent("abc", cfg) is True
    assert net.calls[1]["path"] == "/sent?id=abc"
    net.answers["/sent"] = {"sent": False}
    assert deadman.worker_sent("abc", cfg) is False
    net.answers["/sent"] = OSError("offline")
    assert deadman.worker_sent("abc", cfg) is False  # doubt: the PC delivers
    net.answers["/ack"] = OSError("offline")
    assert deadman.ack("abc", cfg) is False


# ---- the race: no doubles ---------------------------------------------------------------------------------------

def run_due(now):
    out = []
    timers.fire_due(now)
    while (j := jobs.run_one(kinds=["outreach.reminder"], now=now)) is not None:
        out.append(j)
    return out


def test_the_pc_delivers_and_acks_right_away(cfg, net):
    fid = reminder(1, "stand up")
    run_due(at(1) + 5)
    assert followups.get(fid)["status"] == "sent"
    assert len(outbox.list(limit=100)) == 1
    paths = [(c["method"], c["path"]) for c in net.calls]
    assert paths == [("GET", f"/sent?id={fid}"), ("POST", "/ack")]
    assert net.calls[1]["body"] == {"id": fid}


def test_a_reminder_the_worker_already_sent_is_not_delivered_again(cfg, net):
    fid = reminder(1, "stand up")
    net.answers["/sent"] = {"sent": True}
    run_due(at(1) + 3600)  # the PC was off; at its next start the reminder fires late
    assert followups.get(fid)["status"] == "sent"
    assert "already sent" in followups.get(fid)["reason"]
    assert outbox.list(limit=100) == []
    assert [c["path"] for c in net.calls] == [f"/sent?id={fid}"]  # and no ack either


def test_offline_the_pc_still_delivers(cfg, net):
    fid = reminder(1, "stand up")
    net.answers["/sent"] = OSError("offline")
    net.answers["/ack"] = OSError("offline")
    run_due(at(1) + 5)
    assert followups.get(fid)["status"] == "sent"
    assert len(outbox.list(limit=100)) == 1


def test_not_configured_the_reminder_path_is_untouched(settings, net):
    fid = reminder(1, "stand up")
    run_due(at(1) + 5)
    assert followups.get(fid)["status"] == "sent"
    assert net.calls == []


# ---- the core timer ---------------------------------------------------------------------------------------------

def test_register_core_adds_a_five_minute_timer_and_the_job(settings):
    res = deadman.register_core(now=NOW)
    assert res == {"jobs": ["deadman.beat"], "timers": ["deadman.beat"]}
    t = timers.get("deadman.beat")
    assert (t["kind"], t["spec"], t["catch_up"]) == ("interval", "5m", "skip")
    assert jobs.handler("deadman.beat") is not None
    assert "waggle.deadman" in __import__("waggle.core.plugins", fromlist=["MODULES"]).MODULES


def test_the_beat_job_is_quiet_when_not_set_up(settings, net):
    assert deadman.beat_job({}) == {"skipped": "not configured"}
    assert net.calls == []


# ---- commands ---------------------------------------------------------------------------------------------------

def test_setup_makes_a_token_once_and_never_prints_it(settings, capsys):
    text = deadman.setup()
    token = deadman.token()
    assert len(token) >= 32
    assert token not in text and "made and kept" in text
    assert "wrangler login" in text and "deadman-setup.ps1" in text
    assert "already in Credential Manager" in deadman.setup()
    assert deadman.token() == token
    deadman.setup(new_token=True)
    assert deadman.token() != token


def test_seturl_checks_and_saves(settings):
    with pytest.raises(deadman.DeadmanError):
        deadman.seturl("http://example.com")
    assert deadman.seturl(URL + "/") == URL
    assert Settings.load().deadman_url == URL


def test_secret_refuses_a_terminal_and_pipes_otherwise(settings, monkeypatch, capsys):
    wincred.write(deadman.CRED, "waggle", "tok-" + "y" * 40)

    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdout", Tty())
    assert deadman.main(["secret", "token"]) == 2
    assert sys.stdout.getvalue() == ""
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert deadman.main(["secret", "token"]) == 0
    assert out.getvalue() == "tok-" + "y" * 40


def test_secret_values_come_from_the_existing_stores(settings, monkeypatch):
    from waggle.core import telegram
    monkeypatch.setattr(telegram, "load_token", lambda env_file=None: "bot-token")
    monkeypatch.setattr(telegram, "home_chat", lambda env_file=None: "12345")
    assert deadman.secret_value("telegram_token") == "bot-token"
    assert deadman.secret_value("telegram_chat") == "12345"
    assert deadman.secret_value("ntfy_topic").startswith("waggle-")
    monkeypatch.setattr(telegram, "home_chat", lambda env_file=None: None)
    with pytest.raises(deadman.DeadmanError):
        deadman.secret_value("telegram_chat")


def test_status_reports_the_worker(cfg, net, capsys):
    net.answers["/status"] = {"age_s": 40, "queued": 2, "telegram": True, "ntfy": False, "down": None}
    deadman.beat(NOW, cfg)
    st = deadman.status(cfg, NOW + 90, urlopen=net)
    assert st["configured"] and st["last_ok_age"] == "1 min ago"
    assert st["worker"]["queued"] == 2
    assert deadman.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Set up:      yes" in out and "2 reminder(s) queued" in out and "ntfy MISSING" in out
    assert "x" * 43 not in out
