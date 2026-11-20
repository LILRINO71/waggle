"""Every child the core, its detached workers and its watchdog start is windowless and leaves the busy cursor alone.

Measured with tools/window_probe.py before the fix: a console program started without CREATE_NO_WINDOW by a
process without a console opened a Windows Terminal window and took the focus (outreach.look: ~80 per pass,
`claude mcp list` at every chat start), and a detached pythonw worker started by the core showed the
app-starting cursor for ~0.5 s. These tests pin the mechanisms; tools/flicker_check.py proves the result."""
import copy
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from waggle.core import control, procs, settings, store

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="windows and the cursor are Windows things")

NO_WINDOW = 0x08000000
DETACHED = 0x00000008
NEW_CONSOLE = 0x00000010
OFF_FEEDBACK = 0x80


@pytest.fixture(autouse=True)
def _no_leak():
    yield
    procs.uninstall_quiet_children()
    store.close_all()


# ---- the default-flags wrapper -------------------------------------------------------------------------------

def test_quiet_flags_adds_no_window_but_respects_a_chosen_console():
    assert procs.quiet_flags(0) == NO_WINDOW
    assert procs.quiet_flags(0x200) == 0x200 | NO_WINDOW
    assert procs.quiet_flags(NO_WINDOW) == NO_WINDOW
    assert procs.quiet_flags(DETACHED | 0x200) == DETACHED | 0x200  # no console at all: nothing to hide
    assert procs.quiet_flags(NEW_CONSOLE) == NEW_CONSOLE  # a window the caller asked for


def test_quiet_startupinfo_forces_feedback_off_on_a_copy():
    base = subprocess.STARTUPINFO()
    base.dwFlags = subprocess.STARTF_USESHOWWINDOW
    base.wShowWindow = 0
    out = procs.quiet_startupinfo(base)
    assert out is not base and base.dwFlags == subprocess.STARTF_USESHOWWINDOW
    assert out.dwFlags & OFF_FEEDBACK and out.dwFlags & subprocess.STARTF_USESHOWWINDOW and out.wShowWindow == 0
    assert procs.quiet_startupinfo().dwFlags & OFF_FEEDBACK


def _record_popen(monkeypatch):
    seen = []

    def fake_init(self, *args, **kwargs):
        seen.append((args, copy.copy(kwargs)))
        raise OSError("recorded")  # nothing is started
    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    return seen


def test_installed_wrapper_adds_both_flags_to_popen_run_and_check_output(monkeypatch):
    seen = _record_popen(monkeypatch)
    assert procs.install_quiet_children() is True and procs.quiet_installed()
    assert procs.install_quiet_children() is False  # idempotent
    for call in (lambda: subprocess.Popen(["x"]), lambda: subprocess.run(["x"]),
                 lambda: subprocess.check_output(["x"]), lambda: subprocess.Popen(["x"], creationflags=0x200)):
        with pytest.raises(OSError, match="recorded"):
            call()
    assert len(seen) == 4
    for args, kw in seen:
        assert kw["creationflags"] & NO_WINDOW
        assert kw["startupinfo"].dwFlags & OFF_FEEDBACK
    assert seen[3][1]["creationflags"] == 0x200 | NO_WINDOW


def test_installed_wrapper_keeps_a_chosen_console_and_the_callers_startupinfo(monkeypatch):
    seen = _record_popen(monkeypatch)
    procs.install_quiet_children()
    info = subprocess.STARTUPINFO()
    info.dwFlags = subprocess.STARTF_USESHOWWINDOW
    with pytest.raises(OSError):
        subprocess.Popen(["x"], creationflags=NEW_CONSOLE, startupinfo=info)
    _, kw = seen[0]
    assert kw["creationflags"] == NEW_CONSOLE
    assert kw["startupinfo"].dwFlags == subprocess.STARTF_USESHOWWINDOW | OFF_FEEDBACK
    assert info.dwFlags == subprocess.STARTF_USESHOWWINDOW  # the caller's object is not changed


def test_uninstall_restores_popen(monkeypatch):
    seen = _record_popen(monkeypatch)
    procs.install_quiet_children()
    procs.uninstall_quiet_children()
    assert not procs.quiet_installed()
    with pytest.raises(OSError):
        subprocess.Popen(["x"])
    assert "creationflags" not in seen[0][1]


def test_a_real_child_still_runs_and_its_output_is_captured():
    procs.install_quiet_children()
    out = subprocess.run([sys.executable, "-c", "print('quiet')"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "quiet"


# ---- who installs it -------------------------------------------------------------------------------------------

def _run_module_as_main(module, argv, tmp_path):
    """Run `python -m module argv` with install_quiet_children replaced by a recorder; prints what it saw."""
    code = (
        "import runpy, sys\n"
        "import waggle.core.procs as p\n"
        "calls = []\n"
        "p.install_quiet_children = lambda: calls.append(1)\n"
        f"sys.argv = [{module!r}] + {argv!r}\n"
        "try:\n"
        f"    runpy.run_module({module!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('INSTALLED', len(calls))\n")
    env = dict(os.environ, WAGGLE_HOME=str(tmp_path / "home"), PYTHONPATH=str(REPO))
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env, capture_output=True, text=True,
                         timeout=60, creationflags=NO_WINDOW)
    return out.stdout


def test_the_core_command_line_and_the_job_worker_install_it(tmp_path):
    assert "INSTALLED 1" in _run_module_as_main("waggle.core", ["watch", "--stop"], tmp_path)
    assert "INSTALLED 1" in _run_module_as_main("waggle.core.jobrun", [], tmp_path)


def test_runtime_main_installs_it_before_the_core_runs(monkeypatch):
    from waggle.core import runtime
    order = []
    monkeypatch.setattr(procs, "install_quiet_children", lambda: order.append("install"))
    monkeypatch.setattr(runtime.Core, "run", lambda self: order.append("run") or 0)
    assert runtime.main() == 0 and order == ["install", "run"]


# ---- detached spawns and restarts -----------------------------------------------------------------------------

def _record_spawns(monkeypatch):
    seen = []

    class Fake:
        pid = 4242

        def __init__(self, args, **kw):
            seen.append((args, kw))
    monkeypatch.setattr(subprocess, "Popen", Fake)
    return seen


def test_spawn_detached_has_no_console_and_forces_the_busy_cursor_off(monkeypatch, tmp_path):
    seen = _record_spawns(monkeypatch)
    assert procs.spawn_detached(["pythonw", "-m", "waggle.core.jobrun", "1", "o"], cwd=tmp_path,
                                log_path=tmp_path / "j.log") == 4242
    _, kw = seen[0]
    assert kw["creationflags"] & DETACHED and kw["startupinfo"].dwFlags & OFF_FEEDBACK


def test_spawn_detached_keeps_the_flags_when_breakaway_is_refused(monkeypatch, tmp_path):
    seen = []

    class Fake:
        pid = 7

        def __init__(self, args, **kw):
            seen.append(kw)
            if len(seen) == 1:
                raise OSError("no breakaway")
    monkeypatch.setattr(subprocess, "Popen", Fake)
    procs.spawn_detached(["pythonw"], cwd=tmp_path, log_path=tmp_path / "j.log")
    assert len(seen) == 2
    assert all(kw["creationflags"] & DETACHED and kw["startupinfo"].dwFlags & OFF_FEEDBACK for kw in seen)
    assert not seen[1]["creationflags"] & 0x01000000


def test_the_dispatcher_starts_a_job_worker_through_spawn_detached(monkeypatch):
    from waggle.core import jobs, runtime
    store.connect()
    jobs.register("quiet.far", lambda payload: None, detached=True)
    jobs.enqueue("quiet.far", {})
    started = []
    monkeypatch.setattr(procs, "spawn_detached", lambda args, **kw: started.append((args, kw)) or 99)
    core = runtime.Core(workers=1)
    assert core.dispatch_one() is True
    args, kw = started[0]
    assert args[1:3] == ["-m", "waggle.core.jobrun"] and Path(args[0]).name.lower().startswith("python")


def test_the_watchdog_restart_starts_the_core_through_spawn_detached_and_kills_quietly(monkeypatch):
    started = []
    monkeypatch.setenv("WAGGLE_CORE_NO_TASK", "1")
    monkeypatch.setattr(procs, "spawn_detached", lambda args, **kw: started.append(args) or 5)
    monkeypatch.setattr(control, "running_pid", lambda: None)
    control.start(wait_s=0)
    assert started and started[0][1:] == ["-m", "waggle.core", "run"]
    seen = []
    monkeypatch.setattr(procs, "same_process", lambda pid, ctime=None: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append((args, kw)))
    assert procs.kill_tree(123, None) is True
    args, kw = seen[0]
    assert args[0] == "taskkill" and kw["creationflags"] & NO_WINDOW and kw["startupinfo"].dwFlags & OFF_FEEDBACK


def test_schtasks_calls_are_windowless(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append((args, kw)) or
                        subprocess.CompletedProcess(args, 0))
    monkeypatch.delenv("WAGGLE_CORE_NO_TASK", raising=False)
    monkeypatch.delenv("WAGGLE_HOME", raising=False)
    monkeypatch.setattr(settings, "home", lambda: Path.home() / ".waggle")
    assert control._use_task() is True
    assert seen[0][0][0] == "schtasks" and seen[0][1]["creationflags"] & NO_WINDOW


# ---- the long-lived watchdog ------------------------------------------------------------------------------------

def test_hang_recovery_fits_in_two_minutes():
    worst = control.WATCH_STALE_S + control.WATCH_EVERY_S + control.STALE_STOP_S + control.START_TIMEOUT_S
    assert worst <= 120, worst
    assert control.WATCHDOG_STALL_S >= control.STOP_TIMEOUT_S + control.START_TIMEOUT_S + 30


def test_a_healthy_core_costs_the_loop_no_process_start(monkeypatch):
    spawned = []
    monkeypatch.setattr(procs, "spawn_detached", lambda *a, **k: spawned.append(a) or 1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(control, "running_pid", lambda: 4321)
    monkeypatch.setattr(control, "core_info", lambda: {"pid": 4321, "started": time.time()})
    monkeypatch.setattr(control, "heartbeat", lambda: {"pid": 4321, "ts": time.time()})
    store.connect()
    sleeps = []
    assert control.watch_loop(interval_s=1, sleep=lambda s: sleeps.append(s), max_passes=4) == 0
    assert spawned == [] and len(sleeps) == 6  # 3 gaps of 1 s in 0.5 s steps
    assert control.watchdog_info() is None  # cleaned up on exit


def test_the_loop_writes_its_beat_each_pass_and_stops_on_the_stop_file(monkeypatch):
    results = iter(["ok", "ok", "restarted"])
    monkeypatch.setattr(control, "watch", lambda stale_s=50.0: next(results))
    store.connect()
    beats = []

    def sleep(step):
        beats.append(control.watchdog_info())
        if len(beats) >= 3:
            settings.path("watchdog.stop").write_text("1")
    assert control.watch_loop(interval_s=0.5, sleep=sleep) == 0
    assert beats[0]["pid"] == os.getpid() and beats[0]["last"] == "ok" and beats[2]["last"] == "restarted"
    assert not settings.path("watchdog.stop").exists()


def test_a_second_watchdog_is_refused(monkeypatch):
    first = procs.InstanceLock(settings.path("watchdog.lock"))
    assert first.acquire()
    try:
        assert control.watch_loop(interval_s=0.1, max_passes=1) == control.EXIT_ALREADY_RUNNING
    finally:
        first.release()


def test_a_failing_pass_does_not_end_the_watchdog(monkeypatch):
    calls = []

    def flaky(stale_s=50.0):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "ok"
    monkeypatch.setattr(control, "watch", flaky)
    assert control.watch_loop(interval_s=0.1, sleep=lambda s: None, max_passes=3) == 0
    assert len(calls) == 3


def test_a_stalled_pass_ends_the_watchdog_so_the_task_restarts_it(monkeypatch):
    release = threading.Event()
    exits = []
    monkeypatch.setattr(control, "watch", lambda stale_s=50.0: release.wait(10) and "ok")
    runner = threading.Thread(target=lambda: control.watch_loop(
        interval_s=0.1, stall_s=0.4, max_passes=1, exit_fn=lambda code: exits.append(code)))
    runner.start()
    deadline = time.time() + 8
    while not exits and time.time() < deadline:
        time.sleep(0.05)
    release.set()
    runner.join(10)
    assert exits == [1]


def test_two_passes_never_overlap():
    gate = procs.InstanceLock(settings.path("watch.lock"))
    assert gate.acquire()
    try:
        assert control.watch() == "busy"
    finally:
        gate.release()
