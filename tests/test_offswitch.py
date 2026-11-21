"""The one off switch (the file OFF in the Waggle home): reading and writing it, a damaged file, the watchdog that
neither restarts nor alerts while it is set and switches Waggle back on when `--for` runs out, a core or PC voice
started at logon that leaves at once, `off`/`on` ordering, the status text and the gateway's one-line answer.
Everything runs against fakes in a temporary home: no real process, scheduled task, Telegram or Hermes."""
import importlib.util
import threading
import types
from pathlib import Path

import pytest

from waggle.core import control, events, offswitch, procs, runtime, settings, store

T0 = 1_800_000_000.0  # a fixed "now"


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.close_all()


@pytest.fixture
def fakes(monkeypatch):
    """No real core, PC voice, gateway or task: what `off` and `on` would do is recorded in `calls`."""
    class Calls(list):
        state = None

    calls = Calls()
    state = {"pcvoice": False, "core": False}

    def stop(timeout_s=0):
        calls.append("stop core")
        state["core"] = False
        return True

    def start(wait_s=0):
        if offswitch.is_off():
            calls.append("start refused")
            return None
        calls.append("start core")
        state["core"] = True
        return 4242

    monkeypatch.setattr(control, "stop", stop)
    monkeypatch.setattr(control, "start", start)
    monkeypatch.setattr(control, "_pcvoice_running", lambda: state["pcvoice"])
    monkeypatch.setattr(control, "_stop_pcvoice", lambda timeout_s=0: calls.append("stop pcvoice") or True)
    monkeypatch.setattr(control, "_start_pcvoice", lambda: calls.append("start pcvoice") or True)
    monkeypatch.setattr(control, "_stop_gateway_stack",
                        lambda: calls.append("stop gateway") or {"gateway": True, "kokoro": True, "dashboard": True})
    monkeypatch.setattr(control, "_start_gateway_stack", lambda: calls.append("start gateway") or True)
    calls.state = state
    return calls


def _damage(content):
    settings.path(offswitch.FILE).write_bytes(content)


# ---- reading and writing the switch ------------------------------------------------------------------------

def test_no_file_means_on():
    s = offswitch.read()
    assert not s.present and not s.active() and not offswitch.is_off()
    assert offswitch.describe(s) == "Waggle switch: ON (no off switch set)"


def test_write_then_read_round_trips_and_clear_removes_it():
    s = offswitch.write(by="owner", until=T0 + 7200, all=True, restart=["pcvoice"], reason="meeting", now=T0)
    assert s.present and s.valid and s.since == T0 and s.until == T0 + 7200 and s.by == "owner" and s.all
    assert s.restart == ("pcvoice",) and s.reason == "meeting"
    assert offswitch.read() == s
    assert s.active(T0 + 7199) and not s.active(T0 + 7200) and s.expired(T0 + 7200) and not s.expired(T0 + 10)
    assert not list(settings.path(".").glob("OFF.tmp")), "the temporary file is moved into place"
    assert '"all": true' in settings.path(offswitch.FILE).read_text(encoding="utf-8"), "Waggle Gateway.vbs looks for this"
    offswitch.clear()
    assert not offswitch.read().present
    offswitch.clear()  # clearing twice is fine


def test_without_until_it_stays_off_until_on():
    s = offswitch.write(now=T0)
    assert s.until == 0 and s.active(T0 + 10 * 365 * 86400) and not s.expired(T0 + 10 * 365 * 86400)


@pytest.mark.parametrize("content", [
    b"", b"   \n", b"not json", b'{"off": true, "since": ', b"[]", b'{"off": false, "since": 1}', b'{"since": 1}',
    b'{"off": true, "until": "tomorrow"}', b'{"off": true, "until": true}', b'{"off": true, "restart": "pcvoice"}',
    b'\xff\xfe\x00bad bytes'])
def test_a_damaged_switch_file_counts_as_on(content):
    """FAIL-SAFE: reminders and mail must not stop silently because the file is damaged."""
    _damage(content)
    s = offswitch.read()
    assert s.present and not s.valid and s.detail
    assert not s.active(T0) and not s.expired(T0) and not offswitch.is_off()
    text = offswitch.describe(s)
    assert text.startswith("Waggle switch: ON") and "damaged and ignored" in text


def test_a_switch_file_with_a_byte_order_mark_is_read():
    _damage(b"\xef\xbb\xbf" + b'{"off": true, "since": 5, "until": null, "by": "hand"}')
    s = offswitch.read()
    assert s.valid and s.active() and s.by == "hand"


def test_an_unreadable_file_counts_as_on(monkeypatch):
    offswitch.write(now=T0)
    real = Path.read_bytes

    def locked(self):
        if self.name == offswitch.FILE:
            raise PermissionError("locked")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", locked)
    s = offswitch.read()
    assert s.present and not s.valid and not s.active()


def test_write_that_cannot_be_read_back_raises(monkeypatch):
    monkeypatch.setattr(offswitch, "read", lambda: offswitch.State(present=True, valid=False, detail="garbled"))
    with pytest.raises(OSError, match="cannot be read back"):
        offswitch.write(now=T0)


@pytest.mark.parametrize("text,seconds", [("2h", 7200), ("30m", 1800), ("90s", 90), ("1d", 86400),
                                          ("1h30m", 5400), (" 1.5h ", 5400), ("2H", 7200)])
def test_durations(text, seconds):
    assert offswitch.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "2", "soon", "h2", "2 hours", "0s", "2h later", None])
def test_bad_durations_are_refused(text):
    with pytest.raises(ValueError):
        offswitch.parse_duration(text)


# ---- the status text ---------------------------------------------------------------------------------------

def test_describe_says_who_since_when_and_until_when():
    forever = offswitch.write(by="owner", now=T0)
    text = offswitch.describe(forever, T0 + 60)
    assert text.startswith("Waggle switch: OFF since") and "by owner" in text and "core and PC voice stopped" in text
    assert "python -m waggle.core on" in text

    timed = offswitch.write(by="owner", until=T0 + 2 * 3600, all=True, now=T0)
    text = offswitch.describe(timed, T0 + 60)
    assert "back on at" in text and "in 2 h" in text and "gateway, Kokoro and dashboard stopped" in text

    ran_out = offswitch.describe(timed, T0 + 3 * 3600)
    assert "ran out" in ran_out and "watchdog switches Waggle back on" in ran_out


def test_gateway_line_names_the_end_and_the_on_command():
    forever = offswitch.write(now=T0)
    assert offswitch.gateway_line(forever, T0) == "Waggle is switched off; send /on to switch it back on."
    timed = offswitch.write(until=T0 + 600, now=T0)
    line = offswitch.gateway_line(timed, T0)
    assert line.startswith("Waggle is switched off until ") and line.endswith("send /on to switch it back on.")


def test_core_status_prints_the_switch_and_is_not_an_error_while_off(capsys):
    assert control.status() == 2
    assert "Waggle switch: ON" in capsys.readouterr().out
    offswitch.write(by="owner", now=T0)
    assert control.status() == 0, "stopped on purpose is not 'not running'"
    out = capsys.readouterr().out
    assert "Waggle switch: OFF" in out and "not running (switched off)" in out


def test_top_level_status_has_the_switch_line(monkeypatch, capsys):
    import json
    from waggle import cli, ollama, quota
    monkeypatch.setattr(ollama, "is_up", lambda st: False)
    monkeypatch.setattr(quota, "status", lambda st: {})
    offswitch.write(now=T0)
    cli.main(["status"])
    assert json.loads(capsys.readouterr().out)["switch"].startswith("Waggle switch: OFF")


# ---- off and on --------------------------------------------------------------------------------------------

def test_off_writes_the_switch_before_it_stops_anything(fakes, monkeypatch):
    seen = []
    monkeypatch.setattr(control, "stop", lambda timeout_s=0: seen.append(offswitch.is_off()) or True)
    result = control.off(now=T0)
    assert result["ok"] and seen == [True], "the watchdog must already see the switch while the core stops"
    assert offswitch.read().active()


def test_off_stops_the_core_and_pc_voice_and_leaves_the_gateway_alone(fakes):
    fakes.state["pcvoice"] = True
    result = control.off(by="owner", now=T0)
    assert result["ok"] and fakes == ["stop core", "stop pcvoice"]
    s = offswitch.read()
    assert s.present and s.until == 0 and not s.all and s.restart == ("pcvoice",)
    assert [e["kind"] for e in events.query("waggle.off")] == ["waggle.off"]
    assert "stopped: core, pcvoice" in control.off_text(result)


def test_off_all_also_stops_the_gateway_stack(fakes):
    result = control.off(all=True, now=T0)
    assert result["ok"] and fakes == ["stop core", "stop pcvoice", "stop gateway"]
    assert offswitch.read().all


def test_off_reports_what_did_not_stop(fakes, monkeypatch):
    monkeypatch.setattr(control, "stop", lambda timeout_s=0: False)
    result = control.off(now=T0)
    assert not result["ok"] and "NOT stopped: core" in control.off_text(result)
    assert offswitch.read().active(), "the switch stays set so nothing restarts it"


def test_off_stops_nothing_when_the_switch_cannot_be_written(fakes, monkeypatch):
    def boom(**kw):
        raise OSError("disk full")
    monkeypatch.setattr(offswitch, "write", boom)
    result = control.off(now=T0)
    assert not result["ok"] and "did not take" in control.off_text(result) and fakes == []


def test_off_twice_keeps_what_the_first_off_stopped(fakes):
    fakes.state["pcvoice"] = True
    control.off(now=T0)
    fakes.state["pcvoice"] = False  # stopped by the first off
    control.off(all=True, now=T0 + 5)
    s = offswitch.read()
    assert s.restart == ("pcvoice",) and s.all


def test_on_removes_the_switch_then_starts_the_core_and_what_off_stopped(fakes):
    fakes.state["pcvoice"] = True
    control.off(now=T0)
    fakes.clear()
    result = control.on()
    assert result["ok"] and fakes == ["start core", "start pcvoice"]
    assert not offswitch.read().present
    assert "Waggle is ON (the switch was set)" in control.on_text(result)
    assert [e["kind"] for e in events.query("waggle.on")] == ["waggle.on"]


def test_on_after_off_all_starts_the_gateway_task_and_skips_a_pc_voice_that_was_not_running(fakes):
    control.off(all=True, now=T0)
    fakes.clear()
    result = control.on()
    assert result["ok"] and fakes == ["start gateway", "start core"]


def test_on_when_it_was_never_off_just_makes_sure_the_core_runs(fakes):
    result = control.on()
    assert result["ok"] and fakes == ["start core"] and "it was not switched off" in control.on_text(result)


def test_on_removes_a_damaged_file_too(fakes):
    _damage(b"junk")
    assert control.on()["ok"] and not offswitch.read().present


def test_on_starts_nothing_when_the_switch_cannot_be_removed(fakes, monkeypatch):
    offswitch.write(now=T0)

    def boom():
        raise PermissionError("locked")
    monkeypatch.setattr(offswitch, "clear", boom)
    result = control.on()
    assert not result["ok"] and "cannot be removed" in control.on_text(result) and fakes == []


def test_machine_wide_stops_only_happen_from_the_real_waggle_home(monkeypatch):
    """`off --all` stops the Hermes gateway and the Kokoro and dashboard ports; a scratch home must never do that."""
    def forbidden(*a, **k):
        raise AssertionError("a machine-wide command ran from a test home")
    monkeypatch.setattr(control.subprocess, "run", forbidden)
    monkeypatch.setattr(procs, "kill_tree", forbidden)
    assert not control._real_home() and not control._use_task("Waggle Gateway")
    out = control._stop_gateway_stack()
    assert list(out.values()) == [True] and "skipped" in next(iter(out))
    assert control._start_gateway_stack() is False


# ---- start() and the watchdog ------------------------------------------------------------------------------

def test_start_refuses_while_off_and_touches_no_process(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("a process was started while Waggle is off")
    monkeypatch.setattr(procs, "spawn_detached", forbidden)
    monkeypatch.setattr(control.subprocess, "run", forbidden)
    offswitch.write(now=T0)
    assert control.start() is None
    assert control.restart() is None, "restart = stop + start; the start is refused"


def test_the_watchdog_does_not_restart_the_core_or_say_anything_while_off(fakes):
    offswitch.write(by="owner", now=T0)
    for i in range(5):
        assert control._watch_pass(50, T0 + i * 10) == "off"
    assert "start core" not in fakes and "start refused" not in fakes
    assert events.query("core.restarted_by_watchdog") == []
    assert [e for e in events.query() if e["kind"] != "schema.migrated"] == [], "no event, so no alert"


def test_the_watchdog_pass_through_watch_also_sees_the_switch(fakes):
    offswitch.write(now=T0)
    assert control.watch(now=T0) == "off"


def test_the_watchdog_still_restarts_a_down_core_when_the_switch_is_damaged_or_gone(fakes):
    assert control._watch_pass(50, T0) == "started" and fakes == ["start core"]
    fakes.clear()
    _damage(b"{broken")
    assert control._watch_pass(50, T0 + 10) == "started" and fakes == ["start core"]


def test_a_timed_off_switches_itself_on_after_the_time(fakes):
    fakes.state["pcvoice"] = True
    control.off(until_s=3600, now=T0)
    fakes.clear()
    assert control._watch_pass(50, T0 + 1800) == "off"
    assert control._watch_pass(50, T0 + 3599) == "off" and fakes == []
    assert control._watch_pass(50, T0 + 3601) == "switched on"
    assert fakes == ["start core", "start pcvoice"]
    assert not offswitch.read().present
    assert [e["data"]["by"] for e in events.query("waggle.on")] == ["timer"]
    fakes.clear()  # the next pass is an ordinary one: nothing is left over from the switch
    assert control._watch_pass(50, T0 + 3611) == "started" and fakes == ["start core"]


def test_an_open_ended_off_is_never_switched_on_by_the_watchdog(fakes):
    control.off(now=T0)
    fakes.clear()
    assert control._watch_pass(50, T0 + 400 * 86400) == "off" and fakes == []


def test_the_long_lived_watchdog_logs_off_once_and_beats_off(fakes, monkeypatch):
    import logging
    offswitch.write(now=T0)
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Catch()
    control.log.addHandler(handler)
    was = control.log.level
    control.log.setLevel(logging.INFO)
    beats = []
    monkeypatch.setattr(control, "_write_beat", lambda started, last: beats.append(last))
    monkeypatch.setattr(control, "_log_to_file", lambda: None)
    try:
        code = control.watch_loop(interval_s=0.0, sleep=lambda s: None, max_passes=4, exit_fn=lambda c: None)
    finally:
        control.log.removeHandler(handler)
        control.log.setLevel(was)
    assert code == 0 and beats == ["off"] * 4
    assert [r for r in records if r.startswith("pass:")] == ["pass: off"], "logged when it starts, not every pass"


# ---- a core or PC voice started at logon -----------------------------------------------------------------

def test_a_core_started_at_logon_exits_at_once_while_off_and_says_why(monkeypatch, capsys):
    import logging
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Catch()
    runtime.log.addHandler(handler)
    was = runtime.log.level
    runtime.log.setLevel(logging.INFO)
    monkeypatch.setattr(runtime, "setup_logging", lambda name="core.log": None)
    started = []
    monkeypatch.setattr(runtime.Core, "startup", lambda self: started.append(1) or False)
    offswitch.write(by="owner", now=T0)
    try:
        assert runtime.Core().run() == runtime.EXIT_SWITCHED_OFF == 0
    finally:
        runtime.log.removeHandler(handler)
        runtime.log.setLevel(was)
    assert started == [], "it did not even take the lock"
    assert any("not started: Waggle is switched off" in r for r in records)
    assert "switched off" in capsys.readouterr().err


def test_a_core_started_at_logon_runs_when_there_is_no_switch_or_it_ran_out_or_is_damaged(monkeypatch):
    monkeypatch.setattr(runtime, "setup_logging", lambda name="core.log": None)
    started = []
    monkeypatch.setattr(runtime.Core, "startup", lambda self: started.append(1) or False)  # False: lock is held
    assert runtime.Core().run() == runtime.EXIT_ALREADY_RUNNING
    offswitch.write(until=1.0, now=0.5)  # ran out long ago
    assert runtime.Core().run() == runtime.EXIT_ALREADY_RUNNING
    _damage(b"???")
    assert runtime.Core().run() == runtime.EXIT_ALREADY_RUNNING
    assert started == [1, 1, 1]


def test_a_running_core_stops_itself_when_the_switch_appears(monkeypatch):
    """Switched off by hand or by another tool while the core runs: the heartbeat tick notices and shuts down."""
    monkeypatch.setattr(runtime, "setup_logging", lambda name="core.log": None)
    core = runtime.Core(cadence=settings.Cadence(heartbeat=0.0, stop_check=0.0))
    shut = []
    monkeypatch.setattr(core, "startup", lambda: True)
    monkeypatch.setattr(core, "heartbeat", lambda: offswitch.write(now=T0))  # the switch appears at the first beat
    monkeypatch.setattr(core, "shutdown", lambda: shut.append(1))
    assert core.run() == 0 and shut == [1]


def test_the_pc_voice_started_at_logon_exits_at_once_while_off(monkeypatch, capsys):
    import logging
    from waggle import pcvoice
    root = logging.getLogger("waggle")
    before = list(root.handlers)
    offswitch.write(now=T0)
    try:
        code = pcvoice.cmd_run(types.SimpleNamespace(wake=None, dry_wake=False, no_hotkey=True, seconds=0.0))
    finally:
        for h in list(root.handlers):
            if h not in before:
                h.close()
                root.removeHandler(h)
    assert code == 0 and "switched off" in capsys.readouterr().out
    assert not pcvoice.running(), "the lock was let go again"
    log_text = (pcvoice.folder() / pcvoice.LOG_FILE).read_text(encoding="utf-8")
    assert "PC voice not started: Waggle is switched off" in log_text


# ---- the gateway answers one line ----------------------------------------------------------------------------

def _plugin():
    path = Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py"
    spec = importlib.util.spec_from_file_location("wp_offswitch", path)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    return p


def test_the_gateway_answers_the_one_line_while_off_and_nothing_when_on():
    p = _plugin()
    assert p._off_reply("hello", "telegram") is None
    assert p.on_gateway_admission(platform="telegram", source={"chat_id": "1"}, message_id=1, text="hi") is None
    offswitch.write(until=T0 + 10 ** 9, now=T0)
    line = p._off_reply("what's the weather", "telegram")
    assert line.startswith("Waggle is switched off until ") and "send /on to switch it back on" in line
    assert p.on_gateway_admission(platform="telegram", source={"chat_id": "1"}, message_id=1, text="hi") == {
        "action": "handled", "reply": line}
    assert p.on_pre_llm_call(session_id="s", user_message="hi", platform="telegram") == {
        "action": "block", "message": line}
    assert p._off_reply("hi", "cron") is None, "background platforms are left to their own path"


def test_slash_on_from_the_gateway_switches_waggle_back_on(monkeypatch):
    p = _plugin()
    done = threading.Event()
    monkeypatch.setattr(control, "on", lambda by="owner", now=None: done.set() or {"ok": True})
    offswitch.write(now=T0)
    for text in ("/on", " /ON ", "waggle on", "/waggle on", "Waggle on."):
        done.clear()
        assert p._off_reply(text, "telegram") == "Switching Waggle back on now."
        assert done.wait(5), text
    for text in ("on", "turn it on", "/once", "/on please"):
        assert "switched off" in p._off_reply(text, "telegram")


def test_a_failing_switch_check_never_blocks_the_gateway(monkeypatch):
    p = _plugin()

    def boom():
        raise RuntimeError("broken")
    monkeypatch.setattr(offswitch, "read", boom)
    assert p._off_reply("hi", "telegram") is None
