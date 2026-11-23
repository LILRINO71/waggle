"""The daily canary: result parsing, timeout, missing venv, alert-once-per-day, timer registration, no-window spawn.

Nothing real is reached: no Telegram, no real test venv. The child is a tiny fake script run with this Python; the
Telegram side (tools/testclient/canary.py) is driven with a fake client on a fake clock."""
import asyncio
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from waggle import canary as cn
from waggle.core import cron, events, jobs, ledger, outbox, plugins, procs, store, timers

NOW = dt.datetime(2026, 11, 20, 12, 30).timestamp()
DAY = 86400

SRC = Path(__file__).resolve().parent.parent / "tools" / "testclient"
spec = importlib.util.spec_from_file_location("canary_script_under_test", SRC / "canary.py")
script_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script_mod)


# ---- a fake child ---------------------------------------------------------------------------------------------

PASS = {"ok": True, "id": "abc123", "cleaned": 4, "seconds": 3.0, "error": "", "checks": [
    {"name": "roundtrip", "ok": True, "seconds": 2.1, "detail": "answered in 2.1 s"},
    {"name": "reminder", "ok": True, "seconds": 70.0, "detail": "delivered after 70.0 s"}]}
NO_PONG = {"ok": False, "id": "abc123", "cleaned": None, "seconds": 91.0, "error": "", "checks": [
    {"name": "roundtrip", "ok": False, "seconds": 90.0,
     "detail": "Waggle didn't answer a test message within 90 s."}]}
NO_REMINDER = {"ok": False, "id": "abc123", "cleaned": None, "seconds": 250.0, "error": "", "checks": [
    {"name": "roundtrip", "ok": True, "seconds": 2.0, "detail": "answered in 2.0 s"},
    {"name": "reminder", "ok": False, "seconds": 150.0,
     "detail": "A reminder asked for in 1 minute didn't arrive within 150 s."}]}


@pytest.fixture
def client(tmp_path):
    """A test-client folder with a saved login; fake_child() writes the script the check will run."""
    base = tmp_path / "tc"
    base.mkdir()
    (base / "owner.session").write_text("x")
    (base / "api.json").write_text("{}")
    return base


def fake_child(tmp_path, body):
    p = tmp_path / "fake_canary.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def printer(data):
    return f"import json, sys\nprint('noise before')\nprint(json.dumps({data!r}))\nprint('trailing noise')\n"


def args(client, script):
    return {"base": client, "python": sys.executable, "script": script}


def rows(kind=None):
    return [r for r in sorted(outbox.list(limit=1000), key=lambda r: r["id"]) if kind in (None, r["kind"])]


# ---- parsing the child's line ----------------------------------------------------------------------------------

def test_parse_result_takes_the_last_result_line_and_ignores_noise():
    out = "starting\n" + json.dumps({"hello": 1}) + "\n" + json.dumps(PASS) + "\nbye\n{broken\n"
    assert cn.parse_result(out)["id"] == "abc123"
    assert cn.parse_result("nothing here") is None
    assert cn.parse_result(json.dumps({"ok": True})) is None  # no checks list
    assert cn.parse_result("") is None and cn.parse_result(None) is None


def test_execute_normalises_a_pass_and_a_failure(tmp_path, client):
    ok = cn.execute(True, **args(client, fake_child(tmp_path, printer(PASS))))
    assert ok["ok"] and [c["name"] for c in ok["checks"]] == ["roundtrip", "reminder"] and ok["id"] == "abc123"
    bad = cn.execute(True, **args(client, fake_child(tmp_path, printer(NO_REMINDER))))
    assert not bad["ok"] and cn.failure_key(bad) == "reminder"
    # a child that says ok but carries a failed check is still a failure
    odd = cn.execute(True, **args(client, fake_child(tmp_path, printer({**PASS, "checks": NO_REMINDER["checks"]}))))
    assert not odd["ok"]


def test_a_child_that_crashes_or_prints_nothing_is_a_failure_with_its_error(tmp_path, client):
    res = cn.execute(True, **args(client, fake_child(tmp_path, "import sys\nsys.stderr.write('Boom: bad thing')\nsys.exit(3)\n")))
    assert not res["ok"] and cn.failure_key(res) == "script"
    assert "exit 3" in res["checks"][0]["detail"] and "Boom: bad thing" in res["checks"][0]["detail"]


def test_a_child_that_runs_too_long_is_killed(tmp_path, client):
    script = fake_child(tmp_path, "import time\ntime.sleep(60)\n")
    res = cn.execute(True, timeout=1.5, **args(client, script))
    assert not res["ok"] and cn.failure_key(res) == "timeout"
    assert "didn't finish within 2 s" in res["checks"][0]["detail"] or "within 1" in res["checks"][0]["detail"]


def test_no_reminder_flag_reaches_the_child(tmp_path, client):
    script = fake_child(tmp_path, "import json, sys\n"
                        "print(json.dumps({'ok': True, 'checks': [{'name': 'roundtrip', 'ok': True, 'seconds': 1, "
                        "'detail': ' '.join(sys.argv[1:])}]}))\n")
    assert cn.execute(True, **args(client, script))["checks"][0]["detail"] == ""
    assert cn.execute(False, **args(client, script))["checks"][0]["detail"] == "--no-reminder"


def test_child_timeout_covers_the_waits():
    assert cn.child_timeout(False) == cn.SESSION_WAIT_S + cn.SETUP_S + 90
    assert cn.child_timeout(True) == cn.SESSION_WAIT_S + cn.SETUP_S + 90 + 150
    assert cn.JOB_TIMEOUT_S > cn.child_timeout(True)


# ---- the test client is not set up -------------------------------------------------------------------------------

def test_missing_venv_does_not_start_anything(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(cn, "spawn", lambda *a, **k: spawned.append(a))
    base = tmp_path / "nothing"
    res = cn.execute(True, base=base, script=SRC / "canary.py")  # the default python is <base>/venv/...
    assert not spawned and not res["ok"] and cn.failure_key(res) == "setup"
    assert "setup.ps1" in res["checks"][0]["detail"] and "Python is missing" in res["checks"][0]["detail"]


def test_missing_login_is_reported_as_setup(tmp_path, client, monkeypatch):
    (client / "owner.session").unlink()
    monkeypatch.setattr(cn, "spawn", lambda *a, **k: pytest.fail("must not spawn"))
    res = cn.execute(True, **args(client, fake_child(tmp_path, "pass\n")))
    assert cn.failure_key(res) == "setup" and "login.py" in res["checks"][0]["detail"]


def test_venv_python_path_follows_the_testclient_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WAGGLE_TESTCLIENT_DIR", str(tmp_path / "elsewhere"))
    assert cn.testclient_dir() == tmp_path / "elsewhere"
    assert cn.venv_python().parts[-3:-1] == ("venv", "Scripts" if os.name == "nt" else "bin")


# ---- recording and the daily alert --------------------------------------------------------------------------------

def test_a_pass_records_the_ledger_and_sends_nothing(tmp_path, client, settings):
    script = fake_child(tmp_path, printer(PASS))
    res = cn.run_check(settings, NOW, **args(client, script))
    assert res["ok"] and res["alert"] == {"sent": False, "why": "passed"}
    assert not rows()
    assert ledger.ready("canary.roundtrip", NOW + 3600) and ledger.ready("canary.reminder", NOW + 3600)
    assert not ledger.ready("canary.roundtrip", NOW + 37 * 3600)  # a check that stops running turns red by itself
    assert [e["kind"] for e in events.query("canary.*")] == ["canary.ok"]
    st = cn.status(NOW)
    assert st["last"]["ok"] and {r["name"] for r in st["ledger"]} == {"canary.roundtrip", "canary.reminder"}
    assert "passed" in cn.format_status(st) and "ready" in cn.format_status(st)


def test_a_failure_sends_one_text_and_one_push_in_plain_words(tmp_path, client, settings):
    res = cn.run_check(settings, NOW, **args(client, fake_child(tmp_path, printer(NO_PONG))))
    assert not res["ok"] and res["alert"]["sent"]
    texts, pushes = rows("text"), rows("push")
    assert len(texts) == 1 and len(pushes) == 1
    assert texts[0]["body"] == pushes[0]["body"] == "Daily check: Waggle didn't answer a test message within 90 s."
    assert pushes[0]["meta"]["urgent"] and "failed" in pushes[0]["meta"]["title"]
    assert not ledger.ready("canary.roundtrip", NOW + 60)
    assert [e["kind"] for e in events.query("canary.*")] == ["canary.failed"]
    assert "FAILED" in cn.format_status(cn.status(NOW))


def test_the_same_failure_alerts_once_a_day(tmp_path, client, settings):
    script = fake_child(tmp_path, printer(NO_PONG))
    cn.run_check(settings, NOW, **args(client, script))
    again = cn.run_check(settings, NOW + 3 * 3600, **args(client, script))
    assert again["alert"] == {"sent": False, "why": "already alerted today"}
    assert len(rows("text")) == 1 and len(rows("push")) == 1
    # a different failure the same day is news
    cn.run_check(settings, NOW + 4 * 3600, **args(client, fake_child(tmp_path, printer(NO_REMINDER))))
    assert len(rows("text")) == 2 and len(rows("push")) == 2
    # and the next day the same failure is told again
    cn.run_check(settings, NOW + DAY, **args(client, script))
    assert len(rows("text")) == 3 and len(rows("push")) == 3
    assert "reminder" in rows("text")[1]["body"]


def test_setup_and_timeout_and_crash_alert_too_and_no_alert_flag_is_quiet(tmp_path, client, settings):
    missing = cn.run_check(settings, NOW, base=tmp_path / "none", script=SRC / "canary.py")
    assert missing["alert"]["sent"] and rows("text")[0]["body"].startswith("The daily check couldn't run: ")
    slow = fake_child(tmp_path, "import time\ntime.sleep(60)\n")
    cn.run_check(settings, NOW, timeout=1, **args(client, slow))
    assert "didn't finish" in rows("text")[1]["body"]
    quiet = cn.run_check(settings, NOW + DAY, alert_owner=False, **args(client, fake_child(tmp_path, "pass\n")))
    assert not quiet["ok"] and not quiet["alert"]["sent"] and len(rows("text")) == 2
    assert not ledger.ready("canary.roundtrip", NOW + DAY)


def test_alert_goes_to_the_owner_chat_and_a_notify_error_does_not_hide_the_result(tmp_path, client, settings,
                                                                                  monkeypatch):
    settings.engine_chats = ["telegram:42"]
    cn.run_check(settings, NOW, **args(client, fake_child(tmp_path, printer(NO_PONG))))
    assert rows("text")[0]["chat"] == "telegram:42"
    from waggle import notify

    def boom(*a, **k):
        raise RuntimeError("outbox locked")
    monkeypatch.setattr(notify, "deliver", boom)
    res = cn.run_check(settings, NOW + DAY, **args(client, fake_child(tmp_path, printer(NO_PONG))))
    assert not res["ok"] and "outbox locked" in res["alert"]["why"]
    assert cn.alert_due(res, NOW + DAY)  # not marked as told, so the next run tries again


def test_a_failed_check_leaves_the_reminder_entry_alone_when_it_did_not_run(tmp_path, client, settings):
    cn.run_check(settings, NOW, **args(client, fake_child(tmp_path, printer(PASS))))
    cn.run_check(settings, NOW + 3600, reminder=False, **args(client, fake_child(tmp_path, printer(
        {**PASS, "checks": PASS["checks"][:1]}))))
    assert ledger.ready("canary.reminder", NOW + 7200)


# ---- the core job and its timer -----------------------------------------------------------------------------------

def test_register_core_adds_the_job_and_the_daily_timer_once(settings):
    report = cn.register_core(now=NOW)
    assert report == {"jobs": [cn.KIND], "timers": [cn.TIMER]}
    h = jobs.handler(cn.KIND)
    assert h.detached and h.max_attempts == 1 and h.timeout_s == cn.JOB_TIMEOUT_S
    (t,) = timers.list(origin="canary")
    assert (t["id"], t["kind"], t["spec"], t["catch_up"], t["job_kind"]) == \
        ("canary.daily", "cron", "30 12 * * *", "latest", "canary.run")
    assert t["payload"] == {"reminder": True} and t["max_late_s"] == 9 * 3600
    cn.register_core(now=NOW + 5)
    assert len(timers.list(origin="canary")) == 1
    nxt = dt.datetime.fromtimestamp(cron.next_after(t["spec"], NOW))
    assert (nxt.day, nxt.hour, nxt.minute) == (21, 12, 30)


def test_the_daily_run_is_outside_quiet_hours(settings):
    from waggle.config import Settings
    start, end = Settings().quiet_hours
    assert not (12 >= start or 12 < end)


def test_the_core_loads_the_plug_in(monkeypatch):
    assert "waggle.canary" in plugins.MODULES
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.canary")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    assert plugins.load(record=False) == {"loaded": ["waggle.canary"], "failed": {}}
    assert cn.KIND in jobs.kinds(detached=True)


def test_the_job_handler_reads_the_reminder_flag_and_returns_a_small_report(tmp_path, client, settings):
    script = fake_child(tmp_path, "import json, sys\n"
                        "print(json.dumps({'ok': True, 'checks': [{'name': 'roundtrip', 'ok': True, 'seconds': 1, "
                        "'detail': ' '.join(sys.argv[1:])}]}))\n")
    out = cn.canary_job({"reminder": False, "late_s": 0}, settings=settings, now=NOW, **args(client, script))
    assert out["ok"] and out["alert"]["sent"] is False
    assert json.loads(store.get_meta(cn.LAST_KEY))["reminder"] is False
    assert json.loads(store.get_meta(cn.LAST_KEY))["checks"][0]["detail"] == "--no-reminder"
    bad = cn.canary_job({}, settings=settings, now=NOW, **args(client, fake_child(tmp_path, printer(NO_PONG))))
    assert not bad["ok"] and bad["alert"]["sent"]  # a failed check is a result, not a raised job failure


def test_cli_status_and_run(tmp_path, client, settings, monkeypatch, capsys):
    assert cn.main(["status"]) == 0 and "has not run yet" in capsys.readouterr().out
    seen = {}

    def fake_run_check(**kw):
        seen.update(kw)
        return {"ok": False, "checks": [], "alert": {"sent": False}}
    monkeypatch.setattr(cn, "run_check", fake_run_check)
    assert cn.main(["run", "--no-reminder", "--no-alert"]) == 1
    assert seen == {"reminder": False, "alert_owner": False, "code_path": False}
    assert cn.main(["run"]) == 1 and seen == {"reminder": True, "alert_owner": True, "code_path": False}
    assert cn.main(["run", "--no-reminder", "--no-alert", "--code-path"]) == 1 and seen["code_path"] is True
    assert cn.main(["bogus"]) == 2


# ---- no console window --------------------------------------------------------------------------------------------

def test_the_child_is_started_with_the_no_window_flags(monkeypatch):
    got = {}

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, cmd, **kw):
            got.update(cmd=cmd, **kw)

        def communicate(self, timeout=None):
            return "out", "err"
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    code, out, err, timed_out = cn.spawn("py.exe", "canary.py", ["--no-reminder"], 5)
    assert (code, out, err, timed_out) == (0, "out", "err", False)
    assert got["cmd"] == ["py.exe", "canary.py", "--no-reminder"]
    assert got["creationflags"] == procs.NO_WINDOW
    if os.name == "nt":
        assert got["creationflags"] & 0x08000000
        assert got["startupinfo"] is not None
    assert got["stdin"] == subprocess.DEVNULL and got["env"]["PYTHONIOENCODING"] == "utf-8"


def test_a_timeout_kills_the_whole_tree_with_no_window(monkeypatch):
    killed = []

    class SlowPopen:
        pid = 99
        returncode = None

        def __init__(self, cmd, **kw):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("x", timeout)
            return "partial", ""

        def kill(self):
            killed.append("kill")
    monkeypatch.setattr(subprocess, "Popen", SlowPopen)
    monkeypatch.setattr(procs, "kill_tree", lambda pid, ctime=None: killed.append(("tree", pid)))
    assert cn.spawn("py", "s.py", [], 1) == (None, "partial", "", True)
    assert killed == [("tree", 99), "kill"]


# ---- the Telegram side (tools/testclient/canary.py) with a fake client on a fake clock --------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeTelegram:
    """A chat with the bot. `script(text, now)` returns [(delay_s, text, reply_to_us)] the bot sends back."""

    BOT = NS(id=777)

    def __init__(self, clock, script, authorized=True, boom=None):
        self.clock, self.script, self.authorized, self.boom = clock, script, authorized, boom
        self.msgs, self.next_id, self.deleted, self.log = [], 100, [], []

    async def connect(self):
        self.log.append("connect")
        if self.boom:
            raise self.boom

    async def disconnect(self):
        self.log.append("disconnect")

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, name):
        return self.BOT

    def _add(self, sender, text, at, reply_to=None):
        self.next_id += 1
        m = NS(id=self.next_id, sender_id=sender, raw_text=text, at=at, reply_to=NS(reply_to_msg_id=reply_to)
               if reply_to else None)
        self.msgs.append(m)
        return m

    async def send_message(self, bot, text):
        m = self._add(1, text, self.clock())
        for delay, reply, threaded in self.script(text, self.clock()):
            self._add(self.BOT.id, reply, self.clock() + delay, m.id if threaded else None)
        return m

    async def get_messages(self, bot, limit=30, min_id=0):
        return [m for m in reversed(self.msgs) if m.id > min_id and m.at <= self.clock()][:limit]

    async def delete_messages(self, bot, ids, revoke=False):
        assert revoke is True
        self.deleted.append(sorted(ids))
        self.msgs = [m for m in self.msgs if m.id not in ids]


def healthy(text, now):
    cid = text.split("canary ")[1].split()[0].rstrip(":")
    if "remind me" in text:
        return [(3, f"Okay, I'll remind you in 1 minute to canary {cid}", True),
                (65, f"⏰ canary {cid}", False)]
    return [(4, f"pong {cid}", True)]


def drive(client, clock, **kw):
    return asyncio.run(script_mod.run(client, "WaggleBot", clock=clock, sleep=clock.sleep, cid="abc123", **kw))


def test_the_script_passes_both_checks_and_cleans_up_both_sides():
    clock = Clock()
    tg = FakeTelegram(clock, healthy)
    res = drive(tg, clock)
    assert res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip", "reminder"]
    assert res["checks"][0]["seconds"] >= 4 and 60 < res["checks"][1]["seconds"] < 100
    assert res["cleaned"] == 5 and tg.msgs == [] and len(tg.deleted) == 1  # 2 of ours + pong + ack + reminder
    assert tg.log == ["connect", "disconnect"]
    json.dumps(res)


def test_the_ack_that_mentions_the_id_does_not_count_as_the_reminder():
    clock = Clock()

    def only_ack(text, now):
        return healthy(text, now)[:1] if "remind me" in text else healthy(text, now)
    res = drive(FakeTelegram(clock, only_ack), clock, reminder_wait=120)
    assert not res["ok"] and res["checks"][1]["name"] == "reminder" and not res["checks"][1]["ok"]
    assert "didn't arrive within 120 s" in res["checks"][1]["detail"]


def test_no_answer_fails_with_the_90_s_sentence_and_keeps_the_messages():
    clock = Clock()
    tg = FakeTelegram(clock, lambda text, now: [])
    res = drive(tg, clock)
    assert not res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip"]  # the reminder is not tried
    assert res["checks"][0]["detail"] == "Waggle didn't answer a test message within 90 s."
    assert 90 <= res["checks"][0]["seconds"] < 95 and res["cleaned"] is None and tg.deleted == []


def test_a_wrong_answer_is_reported_with_what_the_bot_said():
    clock = Clock()
    res = drive(FakeTelegram(clock, lambda text, now: [(2, "I am a banana", True)]), clock)
    assert not res["ok"] and "I am a banana" in res["checks"][0]["detail"]


def test_no_reminder_flag_and_keep(monkeypatch):
    clock = Clock()
    tg = FakeTelegram(clock, healthy)
    res = drive(tg, clock, reminder=False, keep=True)
    assert res["ok"] and [c["name"] for c in res["checks"]] == ["roundtrip"] and res["cleaned"] is None
    assert len(tg.msgs) == 2 and tg.deleted == []


def test_unrelated_bot_messages_are_not_deleted():
    clock = Clock()

    def chatty(text, now):
        return [(1, "Mail from your bank", False), (2, f"pong abc123", True)]
    tg = FakeTelegram(clock, chatty)
    res = drive(tg, clock, reminder=False)
    assert res["ok"] and res["cleaned"] == 2
    assert [m.raw_text for m in tg.msgs] == ["Mail from your bank"]


def test_not_logged_in_and_a_dead_network_are_results_not_crashes():
    clock = Clock()
    res = drive(FakeTelegram(clock, healthy, authorized=False), clock)
    assert not res["ok"] and res["error"] == "not_logged_in" and "login.py" in res["checks"][0]["detail"]
    boom = FakeTelegram(clock, healthy, boom=ConnectionError("network is down"))
    res = drive(boom, clock)
    assert not res["ok"] and res["checks"][0]["name"] == "telegram" and "network is down" in res["checks"][0]["detail"]


def test_the_script_matchers():
    assert script_mod.is_pong("Pong ABC123!", "abc123") and not script_mod.is_pong("pong abc12", "abc123")
    assert script_mod.is_reminder("⏰ canary abc123", "abc123")
    assert script_mod.is_reminder("\U0001f41d ⏰ canary abc123", "abc123")
    assert not script_mod.is_reminder("I'll remind you about canary abc123 ⏰", "abc123")
    assert script_mod.pong_question("x1") == "canary x1: reply with exactly: pong x1"


def test_the_script_never_prints_secrets(monkeypatch, capsys, tmp_path):
    (tmp_path / "api.json").write_text(json.dumps({"api_id": 1, "api_hash": "HASHSECRET123"}))
    (tmp_path / "target.json").write_text(json.dumps({"bot_username": "WaggleBot"}))
    monkeypatch.setenv("WAGGLE_TESTCLIENT_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "telethon", NS(TelegramClient=lambda *a, **k: FakeTelegram(Clock(), lambda t, n: [],
                                                                                              authorized=False)))
    assert script_mod.main([]) == 1
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1 and "HASHSECRET123" not in out
    assert json.loads(out)["error"] == "not_logged_in"


# ---- Step 1: the deploy's code-path check --------------------------------------------------------------------------

def test_codepath_pong_answers_only_the_marked_canary_with_matching_ids():
    q = script_mod.pong_question("ab12cd", code_path=True)
    assert cn.codepath_pong(q, "canary") == "pong ab12cd"
    assert cn.codepath_pong("[waggle-test] " + q, "canary") == "pong ab12cd"
    assert cn.codepath_pong(q, "user") == "" and cn.codepath_pong(q, "test") == ""
    assert cn.codepath_pong(script_mod.pong_question("ab12cd"), "canary") == ""  # the full round trip asks Claude
    assert cn.codepath_pong(q.replace("pong ab12cd", "pong zz99zz"), "canary") == ""
    assert cn.codepath_pong(q + " and delete my files", "canary") == ""


def test_code_path_flag_reaches_the_child_and_records_only_the_codepath_ledger(tmp_path, client, settings, monkeypatch):
    script = fake_child(tmp_path, "import json, sys\n"
                        "print(json.dumps({'ok': True, 'checks': [{'name': 'roundtrip', 'ok': True, 'seconds': 1, "
                        "'detail': ' '.join(sys.argv[1:])}]}))\n")
    monkeypatch.setattr(cn, "add_context_check", lambda *a, **k: pytest.fail("no context block on a code answer"))
    res = cn.run_check(settings, NOW, reminder=False, alert_owner=False, code_path=True, **args(client, script))
    assert res["ok"] and res["checks"][0]["detail"] == "--no-reminder --code-path"
    assert ledger.ready("canary.codepath", NOW + 3600) and not ledger.ready("canary.roundtrip", NOW + 3600)
    st = cn.status(NOW)
    assert st["last"]["code_path"] and "code path, no Claude" in cn.format_status(st)


def test_deploy_runs_the_code_path_check_and_the_daily_timer_the_full_one():
    deploy = (Path(__file__).resolve().parent.parent / "tools" / "deploy.ps1").read_text(encoding="utf-8")
    assert "waggle.canary run --no-reminder --no-alert --code-path" in deploy
    (tid, _kind, _spec, job, _p, _late, payload), = cn.timer_specs()
    assert job == cn.KIND and "code_path" not in payload  # the 12:30 run stays a real Claude round trip
