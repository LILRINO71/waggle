"""The core as real OS processes in a temporary Waggle home: start, lock, crash and restart, detached work,
watchdog, timers, outbox delivery, graceful stop, plug-in failures, health and timeouts."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import waggle.config as config
from waggle.core import control, events, health, jobs, ledger, outbox, procs, store, timers

REPO = Path(__file__).resolve().parents[1]
FAST = "heartbeat=1,scheduler=0.3,poll=0.2,leases=0.3,housekeeping=1,stop_check=0.2"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the core targets Windows")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers():
    try:
        return health.fetch("/health", timeout=10)[0] == 200
    except OSError:
        return False


def wait_for(cond, timeout=60.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Harness:
    def __init__(self, env):
        self.env = env
        self.home = Path(config.HOME)
        self.procs = []

    def start(self, plugins="test_runtime_core_plugin", cadence=FAST, extra_env=None):
        env = dict(self.env, WAGGLE_CORE_PLUGINS=plugins, WAGGLE_CORE_CADENCE=cadence, **(extra_env or {}))
        out = open(self.home / f"core-{len(self.procs)}.out", "ab")
        p = subprocess.Popen([sys.executable, "-m", "waggle.core", "run"], cwd=str(REPO), env=env, stdout=out,
                             stderr=subprocess.STDOUT)
        out.close()
        self.procs.append(p)
        wait_for(lambda: (control.heartbeat() or {}).get("pid") == p.pid, what="the core's heartbeat")
        wait_for(lambda: _answers(), what="the health endpoint")
        return p

    def cli(self, *args, timeout=90):
        return subprocess.run([sys.executable, "-m", "waggle.core", *args], cwd=str(REPO), env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def lines(self, name):
        try:
            return (self.home / name).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []


@pytest.fixture
def core(monkeypatch):
    home = Path(config.HOME)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(WAGGLE_HOME=str(home), PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "tests")]),
               WAGGLE_CORE_PORT=str(_free_port()), WAGGLE_CORE_CADENCE=FAST, WAGGLE_CORE_NO_TASK="1",
               WAGGLE_CORE_PLUGINS="test_runtime_core_plugin", WAGGLE_CORE_PLUGINS_ONLY="1",
               # The 1 s accept budget is tested in-process with an injected clock; here a loaded machine
               # must not turn a slow accept into a cancelled turn.
               WAGGLE_CORE_ACCEPT_BUDGET_S="60")
    for key in ("WAGGLE_HOME", "PYTHONPATH", "WAGGLE_CORE_PORT", "WAGGLE_CORE_CADENCE", "WAGGLE_CORE_NO_TASK",
                "WAGGLE_CORE_PLUGINS", "WAGGLE_CORE_PLUGINS_ONLY", "WAGGLE_CORE_ACCEPT_BUDGET_S"):
        monkeypatch.setenv(key, env[key])
    h = Harness(env)
    yield h
    try:
        control.stop(timeout_s=5)
    finally:
        for p in h.procs:
            if p.poll() is None:
                p.kill()
                p.wait(60)
        for row in store.connect().execute("SELECT pid, pid_ctime FROM jobs WHERE pid IS NOT NULL").fetchall():
            if row["pid"] != os.getpid():
                procs.kill_tree(row["pid"], row["pid_ctime"])
        store.close_all()


def _job_done(jid):
    job = jobs.get(jid)
    return job if job and job["state"] in ("done", "dead") else None


# ---- process lifecycle ---------------------------------------------------------------------------------------

def test_start_writes_heartbeat_and_a_second_instance_refuses(core):
    p = core.start()
    hb = control.heartbeat()
    assert hb["pid"] == p.pid and hb["version"] and hb["started"] <= hb["ts"]
    assert control.running_pid() == p.pid
    second = core.cli("run", timeout=60)
    assert second.returncode == 3 and "already running" in second.stderr
    assert control.running_pid() == p.pid and p.poll() is None
    assert [e["data"]["pid"] for e in events.query("core.started")] == [p.pid]


def test_health_needs_the_token(core):
    core.start()
    for path in ("/health", "/metrics", "/nothing"):
        assert health.fetch(path, auth=False)[0] == 401
    code, snap = health.fetch("/health")
    assert code == 200 and snap["healthy"] is True, snap["reasons"]
    assert snap["guard"]["status"] == "not approved" and not snap["guard"]["frozen"]
    assert snap["ready"]["safety_core"] is False
    for key in ("heartbeat_age_s", "loops", "queues", "oldest_queued_job_age_s", "oldest_queued_outbox_age_s",
                "dead_jobs_24h", "failed_sends_24h", "ready", "last_catch_up"):
        assert key in snap
    assert {"scheduler", "sender", "dispatcher", "leases", "housekeeping", "guard", "worker-0"} <= set(snap["loops"])
    assert health.fetch("/metrics?days=3")[0] == 200
    assert health.fetch("/nothing")[0] == 404
    status = core.cli("status")
    assert status.returncode == 0 and "healthy" in status.stdout, status.stdout


def test_status_exit_codes(core):
    assert core.cli("status").returncode == 2
    core.start()
    status = core.cli("status")
    assert status.returncode == 0, status.stdout
    store.set_meta("core.unhealthy", json.dumps({"reasons": ["test says so"], "restart": False}))
    result = core.cli("status")
    assert result.returncode == 1 and "test says so" in result.stdout


def test_killed_mid_job_the_job_is_recovered_and_completes_once(core):
    p = core.start()
    jid = jobs.enqueue("t.slow", {})
    wait_for(lambda: "slow start attempts=0" in core.lines("runs.log"), what="the first attempt")
    p.kill()
    p.wait(60)
    core.start()
    job = wait_for(lambda: _job_done(jid), what="the job to finish")
    assert job["state"] == "done" and job["attempts"] == 1 and job["result"] == {"attempts": 1}
    assert core.lines("runs.log").count("slow done") == 1
    retried = [e for e in events.query("job.retried") if e["ref"] == str(jid)]
    assert len(retried) == 1 and retried[0]["data"]["reason"] == "crashed"


def test_detached_job_survives_a_core_restart_and_is_recorded_once(core):
    p = core.start()
    jid = jobs.enqueue("t.detached", {"sleep": 4})
    spawned = wait_for(lambda: [e for e in events.query("job.spawned") if e["ref"] == str(jid)], what="spawn")
    worker = spawned[0]["data"]["pid"]
    wait_for(lambda: jobs.get(jid)["pid"] == worker, what="the worker pid on the job")
    p.kill()
    p.wait(60)
    assert procs.alive(worker)
    core.start()
    job = wait_for(lambda: _job_done(jid), what="the detached job to finish")
    assert job["state"] == "done" and job["attempts"] == 0 and job["result"] == {"pid": worker}
    assert core.lines("runs.log").count(f"detached done {worker}") == 1
    assert len([x for x in core.lines("runs.log") if x.startswith("detached start")]) == 1


def test_watchdog_restarts_a_core_with_a_stale_heartbeat(core):
    assert control.WATCH_STALE_S == 50
    p = core.start(cadence=FAST.replace("heartbeat=1", "heartbeat=3600"))
    hb = control.heartbeat()
    store.set_meta("heartbeat", json.dumps({**hb, "ts": time.time() - 40}))
    assert "ok" in core.cli("watch").stdout
    assert control.running_pid() == p.pid
    store.set_meta("heartbeat", json.dumps({**hb, "ts": time.time() - 80}))
    out = core.cli("watch")
    assert out.returncode == 0 and "restarted" in out.stdout, out.stdout + out.stderr
    assert p.wait(60) == 0
    new = control.running_pid()
    assert new and new != p.pid
    event = events.query("core.restarted_by_watchdog")[-1]
    assert event["data"]["old_pid"] == p.pid and event["data"]["new_pid"] == new
    assert event["data"]["reason"].startswith("heartbeat")
    assert "ok" in core.cli("watch").stdout
    assert control.running_pid() == new
    assert control.stop()
    assert "started" in core.cli("watch").stdout
    assert control.running_pid() not in (None, new)



def _suspend(pid):
    """Freeze every thread of a process, like a hang the core cannot notice itself."""
    import ctypes
    k32, ntdll = ctypes.WinDLL("kernel32"), ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    ntdll.NtSuspendProcess.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    assert h
    ntdll.NtSuspendProcess(h)
    k32.CloseHandle(h)


def test_watchdog_kills_and_replaces_a_hung_core(core):
    p = core.start()
    _suspend(p.pid)
    time.sleep(3)
    out = core.cli("watch", "--stale-s", "2")
    assert "restarted" in out.stdout, out.stdout + out.stderr
    assert p.wait(60) != 0  # killed, not stopped: a frozen process never sees the stop file
    new = control.running_pid()
    assert new and new != p.pid
    assert events.query("core.restarted_by_watchdog")[-1]["data"]["reason"].startswith("heartbeat")

# ---- work through a running core ------------------------------------------------------------------------------

def test_a_timer_fires_through_the_core_into_a_handler(core):
    core.start()
    tid = timers.once(time.time() + 1, "t.echo", {"n": 7})
    wait_for(lambda: any(x.startswith("echo 7 late=") for x in core.lines("runs.log")), what="the timer job")
    assert timers.get(tid)["enabled"] is False
    job = jobs.list(kind="t.echo")[0]
    assert job["state"] == "done" and job["idem_key"].startswith(f"timer:{tid}:")


def test_outbox_rows_are_sent_with_their_ids_and_a_failure_is_retried_once(core):
    core.start()
    hello = outbox.enqueue("text", "hello", chat="c")
    flaky = outbox.enqueue("text", "fail-once", chat="c")
    wait_for(lambda: outbox.get(flaky)["state"] == "sent" and outbox.get(hello)["state"] == "sent", what="sends")
    assert outbox.get(hello)["provider_msg_id"] == f"fake-{hello}"
    row = outbox.get(flaky)
    assert row["provider_msg_id"] == f"fake-{flaky}" and row["attempts"] == 2
    sent = core.lines("sent.log")
    assert sent.count(f"sent {hello}") == 1 and sent.count(f"sent {flaky}") == 1
    retry = [e for e in events.query("outbox.retry") if e["ref"] == str(flaky)]
    assert len(retry) == 1 and "429" in retry[0]["data"]["error"]


def test_a_row_without_transport_stays_queued_with_one_event(core):
    core.start()
    oid = outbox.enqueue("push", "no transport for push in this core")
    wait_for(lambda: events.query("outbox.no_transport", ref=oid), what="the no-transport event")
    time.sleep(1.5)
    assert outbox.get(oid)["state"] == "queued" and outbox.get(oid)["attempts"] == 0
    assert len(events.query("outbox.no_transport", ref=oid)) == 1


def test_graceful_stop_finishes_an_in_flight_send(core):
    p = core.start()
    oid = outbox.enqueue("text", "slow")
    wait_for(lambda: f"begin {oid}" in core.lines("sent.log"), what="the send to begin")
    result = core.cli("stop")
    assert result.returncode == 0
    assert p.wait(60) == 0
    row = outbox.get(oid)
    assert row["state"] == "sent" and row["provider_msg_id"] == f"fake-{oid}"
    assert events.query("core.stopped")[-1]["data"]["pid"] == p.pid
    assert control.running_pid() is None


def test_a_failing_plugin_does_not_stop_the_core(core):
    p = core.start(plugins="test_runtime_core_badplugin,no_such_module_here,test_runtime_core_plugin")
    jid = jobs.enqueue("t.echo", {"n": 1})
    assert wait_for(lambda: _job_done(jid))["state"] == "done"
    failed = {e["ref"]: e["data"]["error"] for e in events.query("core.plugin_failed")}
    assert "broken on purpose" in failed["test_runtime_core_badplugin"]
    assert "ModuleNotFoundError" in failed["no_such_module_here"]
    assert not ledger.ready("plugin:test_runtime_core_badplugin")
    assert ledger.ready("plugin:test_runtime_core_plugin")
    status = core.cli("status")
    assert p.poll() is None and status.returncode == 0, status.stdout


# ---- timeouts ---------------------------------------------------------------------------------------------------

def test_overdue_in_process_job_makes_the_core_unhealthy_and_the_watchdog_restarts_it(core):
    p = core.start()
    jid = jobs.enqueue("t.hang", {})
    wait_for(lambda: [e for e in events.query("job.overdue") if e["ref"] == str(jid)], what="job.overdue")
    code, snap = health.fetch("/health")
    assert snap["healthy"] is False and any("overdue" in r for r in snap["reasons"])
    assert core.cli("status").returncode == 1
    out = core.cli("watch")
    assert "restarted" in out.stdout, out.stdout + out.stderr
    p.wait(60)
    assert events.query("core.restarted_by_watchdog")[-1]["data"]["reason"].startswith("unhealthy")
    assert wait_for(lambda: jobs.get(jid)["attempts"] == 1, what="the hung job to be requeued")


def test_detached_job_past_its_timeout_is_killed_and_failed(core):
    core.start()
    jid = jobs.enqueue("t.dhang", {})
    job = wait_for(lambda: _job_done(jid), what="the timed-out job")
    assert job["state"] == "dead" and "timed out" in job["error"]
    event = [e for e in events.query("job.timeout") if e["ref"] == str(jid)][0]
    assert event["data"]["killed"] is True
    wait_for(lambda: not procs.alive(job["pid"]), what="the worker to be gone")
