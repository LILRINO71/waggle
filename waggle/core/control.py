"""Controlling the core from outside: start, stop, restart, status and the watchdog.

The watchdog runs every minute, so its routine pass stays cheap: it reads core.pid, checks the process and
reads the heartbeat from runtime.db; no git call, no plug-ins, no HTTP request.

The core is identified by core.pid (pid plus process creation time) and its single-instance lock; it is asked
to stop by creating core.stop in the Waggle home, which works for a windowless pythonw process. Starting
goes through the "Waggle Core" scheduled task when it is installed and the default home is in use, so Task
Scheduler keeps supervising it; otherwise the core is started as a detached process."""
import json
import os
import subprocess
import time
from pathlib import Path

from waggle.core import events, procs, settings, store
from waggle.core.settings import REPO

TASK_NAME = "Waggle Core"
STOP_TIMEOUT_S = 20.0
START_TIMEOUT_S = 20.0
WATCH_STALE_S = 75.0  # five missed 15 s heartbeats; with a 1-minute watchdog a hang is restarted in ~2 min
STALE_STOP_S = 5.0  # a core whose heartbeat stopped rarely sees the stop file; kill it soon


def core_info():
    try:
        return json.loads(settings.path("core.pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def running_pid():
    """The pid of the running core, or None."""
    info = core_info()
    if info and procs.same_process(info.get("pid"), info.get("ctime")):
        return int(info["pid"])
    return None


def lock_held():
    return procs.lock_held(settings.path("core.lock"))


def heartbeat():
    value = store.get_meta("heartbeat")
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def _use_task():
    if os.name != "nt" or os.environ.get("WAGGLE_HOME") or os.environ.get("WAGGLE_CORE_NO_TASK"):
        return False
    if settings.home().resolve() != (Path.home() / ".waggle").resolve():
        return False
    try:
        return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, timeout=15,
                              creationflags=procs.NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start(wait_s=START_TIMEOUT_S):
    """Start the core unless one runs; returns its pid (None if it did not come up within wait_s)."""
    pid = running_pid()
    if pid is not None:
        return pid
    begun = time.time()
    if _use_task():
        subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, timeout=15,
                       creationflags=procs.NO_WINDOW)
    else:
        procs.spawn_detached([procs.pythonw(), "-m", "waggle.core", "run"], cwd=REPO,
                             log_path=settings.path("core.out"))
    while time.time() - begun < wait_s:
        pid = running_pid()
        hb = heartbeat()
        if pid is not None and hb and hb.get("pid") == pid and hb.get("ts", 0) >= begun - 1:
            return pid
        time.sleep(0.2)
    return running_pid()


def stop(timeout_s=STOP_TIMEOUT_S):
    """Ask the core to stop; kill it if it has not exited within timeout_s. Returns True when none runs."""
    info = core_info()
    pid = running_pid()
    if pid is None and not lock_held():
        return True
    settings.path("core.stop").write_text(str(time.time()), encoding="utf-8")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if (pid is None or not procs.same_process(pid, info.get("ctime"))) and not lock_held():
            break
        time.sleep(0.2)
    else:
        if pid is not None and procs.kill_tree(pid, info.get("ctime")):
            events.emit("core.killed", source="control", data={"pid": pid, "reason": f"no exit within {timeout_s} s"})
        lock_deadline = time.time() + 10
        while lock_held() and time.time() < lock_deadline:
            time.sleep(0.2)
    try:
        settings.path("core.stop").unlink()
    except FileNotFoundError:
        pass
    return running_pid() is None and not lock_held()


def restart():
    stop()
    return start()


def watch(stale_s=WATCH_STALE_S, now=None):
    """The watchdog pass, safe to run every minute forever: start the core when it is not running, restart it
    when its heartbeat is stale or it asked for a restart. Never starts a second core (start() checks the
    running core and the core's own lock refuses a second instance). Returns what it did."""
    now = store.now_or(now)
    info = core_info() or {}
    pid = running_pid()
    if pid is None:
        if lock_held():
            return "starting"
        new = start()
        events.emit("core.restarted_by_watchdog", source="watchdog",
                    data={"reason": "not running", "old_pid": info.get("pid"), "new_pid": new})
        return "started"
    hb = heartbeat() or {}
    reason, grace = None, STALE_STOP_S
    if hb.get("pid") != pid or not hb.get("ts"):
        if now - float(info.get("started", now)) > stale_s:
            reason = "no heartbeat from the running core"
    elif now - hb["ts"] > stale_s:
        reason = f"heartbeat {now - hb['ts']:.0f} s old"
    else:
        flag = store.get_meta("core.unhealthy")
        flag = json.loads(flag) if flag else None
        if flag and flag.get("restart"):
            reason, grace = "unhealthy: " + "; ".join(flag.get("reasons", [])), STOP_TIMEOUT_S
    if reason is None:
        return "ok"
    stop(timeout_s=grace)
    new = start()
    events.emit("core.restarted_by_watchdog", source="watchdog",
                data={"reason": reason, "old_pid": pid, "new_pid": new})
    return "restarted"


def status_text(snap):
    lines = [f"Waggle core: {'healthy' if snap['healthy'] else 'UNHEALTHY'}"]
    lines += [f"  ! {r}" for r in snap["reasons"]]
    age = snap.get("heartbeat_age_s")
    lines.append(f"pid {snap.get('pid')}  version {snap.get('version')}  heartbeat "
                 f"{'never' if age is None else f'{age:.0f} s ago'}")
    for name, loop in sorted(snap["loops"].items()):
        state = "busy" if loop.get("busy") else ("ok" if loop["alive"] else "STALLED")
        age = loop["age_s"]
        lines.append(f"  loop {name:<13} {state:<8} {'-' if age is None else f'{age:.0f} s'}")
    q = snap["queues"]
    lines.append(f"jobs queued {q['jobs_queued']} running {q['jobs_running']}  outbox queued "
                 f"{q['outbox_queued']} sending {q['outbox_sending']}  inbox waiting "
                 f"{q.get('inbox_waiting', 0)} running {q.get('inbox_running', 0)}")
    lines.append(f"transports: {', '.join(snap.get('transports') or []) or 'none'}  inbound handler: "
                 f"{'yes' if snap.get('inbound_handler') else 'no'}")
    for key in ("oldest_queued_job_age_s", "oldest_queued_outbox_age_s", "oldest_waiting_inbox_age_s"):
        if snap.get(key) is not None:
            lines.append(f"{key.replace('_', ' ')}: {snap[key]:.0f}")
    lines.append(f"last 24 h: dead jobs {snap['dead_jobs_24h']}, failed sends {snap['failed_sends_24h']}")
    guard = snap["guard"]
    lines.append(f"guard: {guard.get('status')}{' (FROZEN)' if guard.get('frozen') else ''}")
    if snap["ready"]:
        lines.append("ready: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in sorted(snap["ready"].items())))
    cu = snap.get("last_catch_up")
    if cu:
        lines.append(f"last catch-up: fired {cu.get('fired')}, late {len(cu.get('late', []))}, "
                     f"skipped {len(cu.get('skipped', []))}, offline since {cu.get('offline_since')}")
    return "\n".join(lines)


def status():
    """Print health; returns 0 healthy, 1 unhealthy, 2 not running."""
    pid = running_pid()
    if pid is None and not lock_held():
        print("Waggle core: not running")
        hb = heartbeat()
        if hb and hb.get("ts"):
            print(f"last heartbeat {time.time() - hb['ts']:.0f} s ago (pid {hb.get('pid')})")
        return 2
    from waggle.core import health
    try:
        code, snap = health.fetch("/health")
    except OSError as e:
        print(f"Waggle core: pid {pid} runs but its health endpoint does not answer ({e})")
        return 1
    if code != 200:
        print(f"Waggle core: health endpoint answered {code}")
        return 1
    print(status_text(snap))
    return 0 if snap["healthy"] else 1
