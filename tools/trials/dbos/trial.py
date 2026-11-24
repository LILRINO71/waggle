"""DBOS trial (Waggle Step 2, decision doc 1.2): one 4-task objective on SQLite.
T1 -> T2 (depends on T1) -> approval wait -> T3 -> T4 (a child workflow that the driver cancels).
Each task is a step that takes STEP_S seconds and records itself in a separate ledger (INSERT OR IGNORE on the
task name: "safe to repeat"). The ledger also logs every start, so re-runs are visible.

    python trial.py run [oid] [--quick] [--auto]   start (or recover) the objective and wait for it
    python trial.py approve [oid] | cancel [oid] | status [oid] | idle SECONDS
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from dbos import DBOS, DBOSClient, DBOSConfig, SetWorkflowID

HERE = Path(__file__).resolve().parent
SYS = f"sqlite:///{(HERE / 'sys.sqlite').as_posix()}"
LEDGER = HERE / "ledger.sqlite"
STEP_S = 1 if "--quick" in sys.argv else 15
VERSION = "trial-v1"  # pinned: a code change must not orphan running workflows (doc 1.2)


def _ledger():
    con = sqlite3.connect(LEDGER, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS runs(task TEXT, ev TEXT, pid INTEGER, t REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS done(task TEXT PRIMARY KEY, pid INTEGER, t REAL)")
    return con


def log(task, ev):
    con = _ledger()
    with con:
        con.execute("INSERT INTO runs VALUES(?,?,?,?)", (task, ev, os.getpid(), time.time()))
    con.close()


@DBOS.step()
def work(task):
    log(task, "start")
    for _ in range(STEP_S):
        time.sleep(1)
    con = _ledger()
    with con:
        new = con.execute("INSERT OR IGNORE INTO done VALUES(?,?,?)", (task, os.getpid(), time.time())).rowcount
    con.close()
    log(task, "done" if new else "done-again(ignored)")
    return task


@DBOS.workflow()
def task_wf(task):
    return work(task)


@DBOS.workflow()
def objective(oid):
    out = {"T1": task_wf(f"{oid}:T1")}
    out["T2"] = task_wf(f"{oid}:T2")  # depends on T1: runs only after it
    log(f"{oid}:T3", "waiting-for-approval")
    answer = DBOS.recv("approval", timeout_seconds=3600)
    out["approval"] = answer
    if answer == "yes":
        out["T3"] = task_wf(f"{oid}:T3")
    with SetWorkflowID(f"{oid}-T4"):
        h = DBOS.start_workflow(task_wf, f"{oid}:T4")
    try:
        out["T4"] = h.get_result()
    except Exception as e:  # cancelled by the driver
        out["T4"] = f"cancelled ({type(e).__name__})"
    return out


def start():
    DBOS(config=DBOSConfig(name="waggle-trial", system_database_url=SYS, application_version=VERSION))
    DBOS.launch()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cmd = args[0] if args else "status"
    oid = args[1] if len(args) > 1 else "obj-1"
    if cmd == "run":
        start()
        with SetWorkflowID(oid):
            h = DBOS.start_workflow(objective, oid)
        print(json.dumps({"pid": os.getpid(), "started": oid}), flush=True)
        if "--auto" in sys.argv:
            time.sleep(3 * STEP_S + 3)
            DBOS.send(oid, "yes", "approval")
        res = h.get_result()
        print(json.dumps({"result": res}), flush=True)
        (HERE / f"{oid}.result.json").write_text(json.dumps(res), encoding="utf-8")
        DBOS.destroy()
    elif cmd in ("approve", "cancel", "status"):
        c = DBOSClient(system_database_url=SYS)
        if cmd == "approve":
            c.send(oid, "yes", "approval")
        elif cmd == "cancel":
            c.cancel_workflow(f"{oid}-T4")
        else:
            print(json.dumps([{"id": w.workflow_id, "status": w.status, "version": w.app_version}
                              for w in c.list_workflows()], default=str))
        c.destroy()
    elif cmd == "idle":
        import psutil
        start()
        p = psutil.Process()
        secs = float(args[1]) if len(args) > 1 else 60
        t0, c0 = time.time(), sum(p.cpu_times()[:2])
        time.sleep(secs)
        cpu = (sum(p.cpu_times()[:2]) - c0) / (time.time() - t0) * 100
        conns = [(c.laddr, c.raddr, c.status) for c in p.net_connections(kind="inet")]
        print(json.dumps({"idle_cpu_percent": round(cpu, 3), "connections": [str(x) for x in conns]}), flush=True)
        DBOS.destroy()


if __name__ == "__main__":
    main()

