import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import waggle.config as config
from waggle.core import events, jobs, procs, store

REPO = Path(__file__).resolve().parents[1]

WORKER = r"""
import json, os, sys, time
from pathlib import Path
from waggle.core import jobs
go = Path(sys.argv[1])
while not go.exists():
    time.sleep(0.002)
owner = jobs.new_owner("w")
got = []
while True:
    job = jobs.claim(owner)
    if job is None:
        break
    got.append(job["id"])
    if not jobs.complete(job["id"], owner, {"owner": owner}):
        raise SystemExit("complete refused for own job")
print(json.dumps({"owner": owner, "got": got}))
"""

CRASHER = r"""
import os, sys
from waggle.core import jobs
job = jobs.claim("crasher", lease_s=float(sys.argv[1]))
print(job["id"], flush=True)
os._exit(3)
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(jobs, "_handlers", {})
    yield
    store.close_all()


def _child_env():
    env = dict(os.environ)
    env["WAGGLE_HOME"] = str(config.HOME)
    env["PYTHONPATH"] = str(REPO)
    return env


def _python(script, *args, **kw):
    return subprocess.Popen([sys.executable, "-c", script, *map(str, args)], cwd=str(REPO), env=_child_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid, p


# ---- basics ------------------------------------------------------------------------------------------------

def test_enqueue_is_idempotent_by_key():
    a = jobs.enqueue("digest", {"x": 1}, idem_key="digest:2026-11-19")
    b = jobs.enqueue("digest", {"x": 2}, idem_key="digest:2026-11-19")
    assert a == b
    assert jobs.get(a)["payload"] == {"x": 1}
    assert len(jobs.list()) == 1


def test_claim_order_priority_and_run_after():
    low = jobs.enqueue("k", priority=0, now=100)
    high = jobs.enqueue("k", priority=5, now=101)
    later = jobs.enqueue("k", priority=9, run_after=500, now=102)
    assert jobs.claim("o", now=200)["id"] == high
    assert jobs.claim("o", now=200)["id"] == low
    assert jobs.claim("o", now=200) is None
    assert jobs.claim("o", now=500)["id"] == later


def test_claim_filters_kinds_and_records_lease_and_process():
    jobs.register("mine", lambda p: None, lease_s=90)
    jobs.enqueue("other", now=1)
    jid = jobs.enqueue("mine", now=2)
    job = jobs.claim("o", kinds=["mine"], now=10)
    assert job["id"] == jid and job["state"] == "running" and job["lease_owner"] == "o"
    assert job["lease_until"] == 100 and job["pid"] == os.getpid()
    assert job["pid_ctime"] == procs.ctime(os.getpid()) is not None
    assert jobs.claim("o", kinds=[]) is None


def test_only_the_owner_can_touch_its_job():
    jid = jobs.enqueue("k", now=0)
    jobs.claim("A", lease_s=60, now=0)
    assert jobs.heartbeat(jid, "B", now=10) is False
    assert jobs.complete(jid, "B", "stolen") is False
    assert jobs.fail(jid, "B", "nope", now=10) is None
    assert jobs.set_pid(jid, "B", os.getpid()) is False
    job = jobs.get(jid)
    assert job["state"] == "running" and job["lease_until"] == 60 and job["result"] is None
    assert jobs.heartbeat(jid, "A", now=30) is True
    assert jobs.get(jid)["lease_until"] == 90
    assert jobs.complete(jid, "A", {"ok": 1}, now=40) is True
    assert jobs.get(jid)["state"] == "done" and jobs.get(jid)["result"] == {"ok": 1}
    assert jobs.complete(jid, "A", "again") is False


def test_stale_owner_after_recovery_changes_nothing():
    jid = jobs.enqueue("k", now=0)
    jobs.claim("A", lease_s=10, now=0)
    pid, _p = _dead_pid()
    assert jobs.set_pid(jid, "A", pid)
    assert jobs.recover(now=20)["requeued"] == 1
    assert jobs.claim("B", now=20)["id"] == jid
    assert jobs.complete(jid, "A", "late result") is False
    assert jobs.heartbeat(jid, "A", now=21) is False
    job = jobs.get(jid)
    assert job["state"] == "running" and job["lease_owner"] == "B" and job["result"] is None


def test_fail_retries_with_backoff_then_dies():
    jid = jobs.enqueue("k", max_attempts=3, now=0)
    jobs.claim("o", now=0)
    assert jobs.fail(jid, "o", "e1", now=0) == "queued"
    job = jobs.get(jid)
    assert job["attempts"] == 1 and job["run_after"] == jobs.RETRY_BASE_S and job["lease_owner"] is None
    assert jobs.claim("o", now=10) is None
    jobs.claim("o", now=30)
    assert jobs.fail(jid, "o", "e2", now=30) == "queued"
    assert jobs.get(jid)["run_after"] == 30 + 2 * jobs.RETRY_BASE_S
    jobs.claim("o", now=100)
    assert jobs.fail(jid, "o", "e3", now=100) == "dead"
    job = jobs.get(jid)
    assert job["state"] == "dead" and job["attempts"] == 3 and job["error"] == "e3"
    assert [e["kind"] for e in events.query("job.*")] == ["job.retried", "job.retried", "job.dead"]
    assert jobs.retry_delay(50) == jobs.RETRY_CAP_S


def test_fail_without_retry_is_terminal():
    jid = jobs.enqueue("k", max_attempts=5, now=0)
    jobs.claim("o", now=0)
    assert jobs.fail(jid, "o", "bad input", retry=False, now=1) == "dead"


def test_run_one_runs_registered_handlers():
    jobs.register("add", lambda p: p["a"] + p["b"])
    jobs.register("boom", lambda p: 1 / 0, max_attempts=1)
    jobs.register("far", lambda p: None, detached=True)
    a = jobs.enqueue("add", {"a": 2, "b": 3}, now=0)
    b = jobs.enqueue("boom", {}, now=1)
    jobs.enqueue("far", {}, now=2)
    assert jobs.run_one(now=5)["result"] == 5 and jobs.get(a)["state"] == "done"
    out = jobs.run_one(now=5)
    assert out["id"] == b and out["state"] == "dead" and "ZeroDivisionError" in out["error"]
    assert jobs.run_one(now=5) is None
    assert jobs.get(a)["max_attempts"] == 3 and jobs.get(b)["max_attempts"] == 1


# ---- crash recovery with real processes ----------------------------------------------------------------------

def test_process_identity_detects_dead_and_reused_pids():
    assert procs.alive(os.getpid())
    own = procs.ctime(os.getpid())
    assert own is not None and procs.ctime(os.getpid()) == own
    assert procs.same_process(os.getpid(), own)
    assert not procs.same_process(os.getpid(), own + 1)
    pid, _p = _dead_pid()
    assert not procs.alive(pid) and procs.ctime(pid) is None
    assert not procs.alive(0) and not procs.alive(None)


def test_crashed_worker_is_requeued_with_attempts_plus_one():
    store.connect()
    jid = jobs.enqueue("k", max_attempts=3, now=time.time())
    p = _python(CRASHER, 1)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 3, err
    assert int(out.strip()) == jid
    job = jobs.get(jid)
    assert job["state"] == "running" and job["pid"] == p.pid and job["pid_ctime"] is not None
    assert jobs.recover(now=time.time() - 3600) == {"requeued": 0, "dead": 0, "adopted": 0}  # lease still valid
    counts = jobs.recover(now=time.time() + 5)
    assert counts == {"requeued": 1, "dead": 0, "adopted": 0}
    job = jobs.get(jid)
    assert job["state"] == "queued" and job["attempts"] == 1 and job["lease_owner"] is None
    retried = events.query("job.retried")[-1]
    assert retried["ref"] == str(jid) and retried["data"]["reason"] == "crashed"
    assert events.query("jobs.recovered")[-1]["data"]["requeued"] == 1


def test_crash_with_attempts_exhausted_goes_dead_with_an_event():
    store.connect()
    jid = jobs.enqueue("k", max_attempts=1, now=time.time())
    p = _python(CRASHER, 1)
    p.communicate(timeout=60)
    assert jobs.recover(now=time.time() + 5)["dead"] == 1
    assert jobs.get(jid)["state"] == "dead"
    assert events.query("job.dead")[-1]["ref"] == str(jid)


def test_live_detached_worker_is_adopted_and_reused_pid_is_dead():
    jid = jobs.enqueue("k", now=0)
    jobs.claim("core-1", lease_s=10, now=0)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert jobs.set_pid(jid, "core-1", child.pid)
        assert jobs.get(jid)["pid_ctime"] == procs.ctime(child.pid)
        assert jobs.recover(now=20) == {"requeued": 0, "dead": 0, "adopted": 1}
        job = jobs.get(jid)
        assert job["state"] == "running" and job["lease_owner"] == "core-1" and job["lease_until"] == 30
        assert events.query("job.adopted")[-1]["data"]["pid"] == child.pid
        assert jobs.heartbeat(jid, "core-1", now=25)
        # Same pid, different creation time: the pid now belongs to some other process.
        store.connect().execute("UPDATE jobs SET pid_ctime = pid_ctime + 1 WHERE id=?", (jid,))
        assert jobs.recover(now=100) == {"requeued": 1, "dead": 0, "adopted": 0}
        assert jobs.get(jid)["state"] == "queued" and jobs.get(jid)["attempts"] == 1
    finally:
        child.kill()
        child.wait()


def test_four_processes_claim_200_jobs_exactly_once(tmp_path):
    store.connect()
    ids = {jobs.enqueue("bulk", {"n": i}) for i in range(200)}
    assert len(ids) == 200
    go = tmp_path / "go"
    workers = [_python(WORKER, go) for _ in range(4)]
    time.sleep(0.5)
    go.write_text("go")
    results = []
    for w in workers:
        out, err = w.communicate(timeout=180)
        assert w.returncode == 0, err
        results.append(json.loads(out))
    claimed = [jid for r in results for jid in r["got"]]
    assert len(claimed) == 200
    assert set(claimed) == ids
    owner_of = {jid: r["owner"] for r in results for jid in r["got"]}
    for jid in ids:
        job = jobs.get(jid)
        assert job["state"] == "done" and job["result"] == {"owner": owner_of[jid]}
    assert sum(1 for r in results if r["got"]) >= 2
