"""Controlling the core from outside: start, stop, restart, status and the watchdog.

The watchdog is one long-lived process (`python -m waggle.core watch --loop`, started at logon) that passes every
10 s and otherwise sleeps, so nothing starts every minute: a process start is a chance for the user to see or feel
something (a window, the busy cursor, a focus change). Its routine pass stays cheap: it reads core.pid, checks the
process and reads the heartbeat from runtime.db; no git call, no plug-ins, no HTTP request. It holds
watchdog.lock (never two watchdogs), writes watchdog.json after every pass, stops when watchdog.stop appears, and
exits non-zero if one pass takes over 2 minutes so Task Scheduler restarts it (the task also repeats every 15
minutes as a backstop; a second instance is ignored by the task and refused by the lock). `watch` alone is one
pass, as before.

The core is identified by core.pid (pid plus process creation time) and its single-instance lock; it is asked
to stop by creating core.stop in the Waggle home, which works for a windowless pythonw process. Starting
goes through the "Waggle Core" scheduled task when it is installed and the default home is in use, so Task
Scheduler keeps supervising it; otherwise the core is started as a detached process.

The one off switch (waggle/core/offswitch.py, the file OFF in the Waggle home): `off` writes it and stops the core
and the PC voice (`--all`: also the gateway, Kokoro and the dashboard); while it is set start() refuses and the
watchdog neither restarts nor alerts; `on` removes it and starts what `off` stopped; a switch with an end time
(`off --for 2h`) is removed by the watchdog itself when the time has passed."""
import json
import logging
import logging.handlers
import os
import subprocess
import threading
import time
from pathlib import Path

from waggle.core import events, offswitch, procs, settings, store
from waggle.core.settings import REPO

TASK_NAME = "Waggle Core"
PCVOICE_TASK = "Waggle PC Voice"
GATEWAY_TASK = "Waggle Gateway"
KOKORO_PORT, DASHBOARD_PORT = 8899, 8897  # what Waggle Gateway.vbs starts besides Ollama and the Hermes gateway
STOP_TIMEOUT_S = 20.0
START_TIMEOUT_S = 20.0
WATCH_STALE_S = 50.0  # three missed 15 s heartbeats; with a 1-minute watchdog a hang is replaced within 2 min
STALE_STOP_S = 5.0  # a core whose heartbeat stopped rarely sees the stop file; kill it soon
WATCH_EVERY_S = 10.0  # the long-lived watchdog's pass interval: a hang is replaced about 60-85 s after it starts
WATCHDOG_STALL_S = 120.0  # a watchdog pass that takes longer ends the watchdog (Task Scheduler restarts it)
EXIT_ALREADY_RUNNING = 3

log = logging.getLogger("waggle.core.watchdog")


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


def _real_home():
    """True only for the owner's real Waggle home (~/.waggle, WAGGLE_HOME unset): machine-wide things (scheduled
    tasks, the Hermes gateway, Kokoro and the dashboard on their ports) are touched from there and nowhere else."""
    if os.name != "nt" or os.environ.get("WAGGLE_HOME"):
        return False
    return settings.home().resolve() == (Path.home() / ".waggle").resolve()


def _use_task(task=TASK_NAME):
    if not _real_home() or os.environ.get("WAGGLE_CORE_NO_TASK"):
        return False
    try:
        return subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True, timeout=15,
                              creationflags=procs.NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start(wait_s=START_TIMEOUT_S):
    """Start the core unless one runs; returns its pid (None if it did not come up within wait_s, or when the
    off switch is set: nothing starts the core while Waggle is off, whoever asks)."""
    pid = running_pid()
    if pid is not None:
        return pid
    if offswitch.is_off():
        log.info("start refused: Waggle is switched off")
        return None
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
    """One watchdog pass: start the core when it is not running, restart it when its heartbeat is stale or it
    asked for a restart. Never starts a second core (start() checks the running core and the core's own lock
    refuses a second instance), and two passes never overlap (watch.lock; the second returns "busy").
    Returns what it did."""
    gate = procs.InstanceLock(settings.path("watch.lock"))
    if not gate.acquire():
        return "busy"
    try:
        return _watch_pass(stale_s, now)
    finally:
        gate.release()


def _watch_pass(stale_s, now):
    now = store.now_or(now)
    switch = offswitch.read()
    if switch.active(now):
        return "off"  # no restart, no event, no alert: the owner switched Waggle off on purpose
    if switch.expired(now):
        on(by="timer", now=now)  # `off --for`: the time is up, Waggle switches itself back on
        return "switched on"
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


# ---- the long-lived watchdog ------------------------------------------------------------------------------

def watchdog_info():
    try:
        return json.loads(settings.path("watchdog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def watchdog_running():
    """The pid of the running long-lived watchdog, or None (it holds watchdog.lock and records itself)."""
    info = watchdog_info()
    if info and procs.same_process(info.get("pid"), info.get("ctime")) and procs.lock_held(
            settings.path("watchdog.lock")):
        return int(info["pid"])
    return None


def _write_beat(started, last):
    path = settings.path("watchdog.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "ctime": procs.own_ctime(), "started": started,
                               "ts": time.time(), "last": last, "interval_s": WATCH_EVERY_S}), encoding="utf-8")
    os.replace(tmp, path)


def _log_to_file():
    if any(getattr(h, "_waggle_watchdog", False) for h in log.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(settings.path("watchdog.log"), maxBytes=200_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    handler._waggle_watchdog = True
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def watch_loop(interval_s=WATCH_EVERY_S, stale_s=WATCH_STALE_S, stall_s=WATCHDOG_STALL_S, sleep=time.sleep,
               max_passes=None, exit_fn=os._exit):
    """Run watch() every `interval_s` until watchdog.stop appears. Returns 0, or EXIT_ALREADY_RUNNING when
    another watchdog holds the lock. `exit_fn(1)` ends the process when a pass (or the sleep) stalls for
    `stall_s`; it is a parameter for tests."""
    _log_to_file()
    lock = procs.InstanceLock(settings.path("watchdog.lock"))
    if not lock.acquire():
        return EXIT_ALREADY_RUNNING
    stop_file = settings.path("watchdog.stop")
    try:
        stop_file.unlink()
    except FileNotFoundError:
        pass
    started = time.time()
    progress = {"at": time.monotonic(), "done": False}

    def stall_guard():
        while not progress["done"]:
            time.sleep(min(5.0, max(0.05, stall_s / 4)))
            if not progress["done"] and time.monotonic() - progress["at"] > stall_s:
                log.error("a watchdog pass stalled for over %s s; exiting so the task restarts it", stall_s)
                exit_fn(1)
                return

    threading.Thread(target=stall_guard, name="watchdog-stall", daemon=True).start()
    log.info("watchdog started (pid %s), pass every %s s", os.getpid(), interval_s)
    passes = 0
    last_result = None
    try:
        while not stop_file.exists():
            progress["at"] = time.monotonic()
            try:
                result = watch(stale_s=stale_s)
            except Exception as e:  # one bad pass must not end the watchdog
                result = f"error: {type(e).__name__}: {e}"
                log.exception("watch pass failed")
            if result not in ("ok", "busy", "off") or (result == "off") != (last_result == "off"):
                log.info("pass: %s", result)  # "off" is logged once when it starts, not every 10 s
            last_result = result
            try:
                _write_beat(started, result)
            except OSError:
                log.exception("could not write watchdog.json")
            passes += 1
            if max_passes is not None and passes >= max_passes:
                break
            waited = 0.0
            while waited < interval_s and not stop_file.exists():
                step = min(0.5, interval_s - waited)
                sleep(step)
                waited += step
                progress["at"] = time.monotonic()
    finally:
        progress["done"] = True
        for name in ("watchdog.stop", "watchdog.json"):
            try:
                settings.path(name).unlink()
            except FileNotFoundError:
                pass
        lock.release()
        log.info("watchdog stopped")
    return 0


def stop_watchdog(timeout_s=15.0):
    """Ask the long-lived watchdog to stop (watchdog.stop); True when none runs afterwards."""
    pid = watchdog_running()
    if pid is None and not procs.lock_held(settings.path("watchdog.lock")):
        return True
    settings.path("watchdog.stop").write_text(str(time.time()), encoding="utf-8")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not procs.lock_held(settings.path("watchdog.lock")):
            return True
        time.sleep(0.2)
    info = watchdog_info() or {}
    if pid is not None and procs.kill_tree(pid, info.get("ctime")):
        time.sleep(0.5)
    try:
        settings.path("watchdog.stop").unlink()
    except FileNotFoundError:
        pass
    return not procs.lock_held(settings.path("watchdog.lock"))


# ---- the off switch: off and on ---------------------------------------------------------------------------

def _pcvoice_running():
    # The lock file name is pcvoice.LOCK_FILE; not imported so that off/on never load the audio modules.
    path = settings.path("pcvoice.lock")
    return path.exists() and procs.lock_held(path)


def _stop_pcvoice(timeout_s=5.0):
    """Ask the PC voice to stop (pcvoice.stop, as `python -m waggle.pcvoice stop` does). True when none runs."""
    if not _pcvoice_running():
        return True
    settings.path("pcvoice.stop").write_text("stop", encoding="ascii")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pcvoice_running():
            return True
        time.sleep(0.1)
    return False


def _start_pcvoice():
    if _pcvoice_running():
        return True
    if _use_task(PCVOICE_TASK):
        subprocess.run(["schtasks", "/Run", "/TN", PCVOICE_TASK], capture_output=True, timeout=15,
                       creationflags=procs.NO_WINDOW)
    else:
        procs.spawn_detached([procs.pythonw(), "-m", "waggle.pcvoice", "run"], cwd=REPO,
                             log_path=settings.path("pcvoice.out"))
    return True


def _listening_pids(port):
    """Pids that listen on 127.0.0.1:port (netstat; Windows only, [] elsewhere or on any error)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20,
                             creationflags=procs.NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(pids)


def _stop_gateway_stack():
    """`off --all`: the Hermes gateway (`hermes gateway stop --yes`), then Kokoro and the dashboard by their ports.
    Returns {name: True/False}. Ollama is left alone (other programs use it). Only from the real Waggle home: these
    are machine-wide (a test or scratch home must never stop the owner's gateway)."""
    if not _real_home():
        return {"gateway stack (skipped: not the real Waggle home)": True}
    done = {}
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    exe = hermes_home / "bin" / "hermes.exe"
    if exe.exists():
        try:
            r = subprocess.run([str(exe), "gateway", "stop", "--yes"], capture_output=True, timeout=60,
                               creationflags=procs.NO_WINDOW, env=dict(os.environ, HERMES_HOME=str(hermes_home)))
            done["gateway"] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            done["gateway"] = False
    for name, port in (("kokoro", KOKORO_PORT), ("dashboard", DASHBOARD_PORT)):
        pids = _listening_pids(port)
        done[name] = all(procs.kill_tree(pid) or not procs.alive(pid) for pid in pids)
    return done


def _start_gateway_stack():
    """`on` after `off --all`: the "Waggle Gateway" task runs the launcher that starts Ollama (if down), Kokoro, the
    dashboard and the Hermes gateway, each only when it is not already up."""
    if not _use_task(GATEWAY_TASK):
        return False
    subprocess.run(["schtasks", "/Run", "/TN", GATEWAY_TASK], capture_output=True, timeout=15,
                   creationflags=procs.NO_WINDOW)
    return True


def off(until_s=None, all=False, by="owner", now=None):
    """Switch Waggle off: write the switch first (so the watchdog cannot restart anything), then stop the core
    gracefully and the PC voice (`all`: and the gateway, Kokoro and the dashboard). `until_s`: seconds from now
    after which the watchdog switches Waggle back on. Returns {"ok", "state", "stopped": {...}, "error"}; when
    the switch cannot be written nothing is stopped."""
    now = store.now_or(now)
    prior = offswitch.read()
    restart = set(prior.restart) if prior.present and prior.valid else set()
    if _pcvoice_running():
        restart.add("pcvoice")
    try:
        state = offswitch.write(by=by, until=(now + until_s) if until_s else 0.0,
                                all=all or (prior.active(now) and prior.all), restart=restart, now=now)
    except OSError as e:
        return {"ok": False, "state": None, "stopped": {}, "error": f"the off switch did not take: {e}"}
    try:
        events.emit("waggle.off", source="control", data={"by": by, "until": state.until or None, "all": state.all})
    except Exception:  # the event log must never keep Waggle from switching off
        log.exception("could not record waggle.off")
    stopped = {"core": stop(), "pcvoice": _stop_pcvoice()}
    if state.all:
        stopped.update(_stop_gateway_stack())
    return {"ok": all_true(stopped), "state": state, "stopped": stopped, "error": ""}


def all_true(mapping):
    return all(mapping.values())


def on(by="owner", now=None):
    """Remove the off switch and start what `off` stopped: the core always, the PC voice when it was running,
    the gateway stack after `off --all`. Returns {"ok", "was": State, "started": {...}, "error"}; when the switch
    cannot be removed nothing is started."""
    was = offswitch.read()
    try:
        offswitch.clear()
    except OSError as e:
        return {"ok": False, "was": was, "started": {}, "error": f"the off switch cannot be removed: {e}"}
    try:
        events.emit("waggle.on", source="control", data={"by": by, "was_off": was.present})
    except Exception:
        log.exception("could not record waggle.on")
    started = {}
    if was.all:
        started["gateway_task"] = _start_gateway_stack()
    started["core"] = start() is not None
    if "pcvoice" in was.restart:
        started["pcvoice"] = _start_pcvoice()
    return {"ok": started["core"], "was": was, "started": started, "error": ""}


def off_text(result):
    if not result["ok"] and result["state"] is None:
        return result["error"]
    text = [offswitch.describe(result["state"])]
    bad = [k for k, v in result["stopped"].items() if not v]
    text.append("stopped: " + (", ".join(k for k, v in result["stopped"].items() if v) or "nothing"))
    if bad:
        text.append("NOT stopped: " + ", ".join(bad) + " (the switch is set, so nothing restarts them)")
    return "\n".join(text)


def on_text(result):
    if result["error"]:
        return result["error"]
    started = ", ".join(k for k, v in result["started"].items() if v)
    failed = [k for k, v in result["started"].items() if not v]
    text = f"Waggle is ON{' (the switch was set)' if result['was'].present else ' (it was not switched off)'}; "
    text += f"started: {started or 'nothing'}"
    if failed:
        text += f"; did not start: {', '.join(failed)}"
    return text


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
    lines.append(f"guard: {guard.get('status')}{' (FROZEN)' if guard.get('frozen') else ''}"
                 + (f" - {guard['detail']}" if guard.get("detail") else ""))
    if snap["ready"]:
        lines.append("ready: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in sorted(snap["ready"].items())))
    cu = snap.get("last_catch_up")
    if cu:
        lines.append(f"last catch-up: fired {cu.get('fired')}, late {len(cu.get('late', []))}, "
                     f"skipped {len(cu.get('skipped', []))}, offline since {cu.get('offline_since')}")
    return "\n".join(lines)


def status():
    """Print the off switch and health; returns 0 healthy (or off on purpose with the core stopped), 1 unhealthy,
    2 not running."""
    switch = offswitch.read()
    print(offswitch.describe(switch))
    pid = running_pid()
    if pid is None and not lock_held():
        print("Waggle core: not running" + (" (switched off)" if switch.active() else ""))
        if switch.active():
            return 0
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
    wd = watchdog_info()
    wpid = watchdog_running()
    print("watchdog: " + (f"running (pid {wpid}), last pass {time.time() - wd['ts']:.0f} s ago" if wpid and wd
                          else "not running (one-shot passes only)"))
    return 0 if snap["healthy"] else 1
