"""Drives trial.py through the decision doc 1.2 cases and prints a JSON verdict."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / "venv" / "Scripts" / "python.exe")
PYW = str(HERE / "venv" / "Scripts" / "pythonw.exe")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
events = []


def note(msg):
    events.append(f"{time.strftime('%H:%M:%S')} {msg}")
    print(events[-1], flush=True)


def spawn(*args, exe=PY, log=None):
    out = open(HERE / (log or f"w{len(events)}.log"), "w", encoding="utf-8")
    return subprocess.Popen([exe, str(HERE / "trial.py"), *args], cwd=HERE, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=NOWIN)


def kill(p, why):
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
    p.wait(10)
    note(f"killed pid {p.pid}: {why}")


def ledger(q, *a):
    try:
        con = sqlite3.connect(HERE / "ledger.sqlite", timeout=30)
        r = con.execute(q, a).fetchall()
        con.close()
        return r
    except sqlite3.OperationalError:
        return []


def has(task, ev):
    return bool(ledger("SELECT 1 FROM runs WHERE task=? AND ev=?", task, ev))


def wait(cond, what, limit=240):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > limit:
            raise SystemExit(f"timeout waiting for {what}")
        time.sleep(0.5)
    note(f"saw: {what}")


def client(*args):
    r = subprocess.run([PY, str(HERE / "trial.py"), *args], cwd=HERE, capture_output=True, text=True,
                       creationflags=NOWIN)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]


def main():
    for f in ("sys.sqlite", "sys.sqlite-wal", "sys.sqlite-shm", "ledger.sqlite"):
        (HERE / f).unlink(missing_ok=True)
    o = "obj-1"
    a = spawn("run", o)
    wait(lambda: has(f"{o}:T1", "start"), "T1 started")
    time.sleep(6)
    kill(a, "1st kill, in the middle of T1")
    b = spawn("run", o)
    wait(lambda: has(f"{o}:T2", "start"), "T2 started")
    time.sleep(6)
    kill(b, "2nd kill, in the middle of T2")
    src = (HERE / "trial.py").read_text(encoding="utf-8")
    (HERE / "trial.py").write_text(src + f"\n# redeploy {time.time()}: an unrelated code change\n", encoding="utf-8")
    note("redeployed: trial.py changed (unrelated line), version pinned")
    c = spawn("run", o)
    wait(lambda: has(f"{o}:T3", "waiting-for-approval"), "waiting for the approval")
    time.sleep(3)
    kill(c, "3rd kill, during the approval wait")
    d = spawn("run", o, log="worker-d.log")
    time.sleep(8)
    note(f"approve: {client('approve', o)}")
    wait(lambda: has(f"{o}:T3", "start"), "T3 started after the approval")
    e = spawn("run", o, log="worker-e.log")
    note("second worker started against the same database")
    wait(lambda: has(f"{o}:T4", "start"), "T4 started")
    time.sleep(3)
    note(f"cancel T4: {client('cancel', o)}")
    for p, name in ((d, "D"), (e, "E")):
        try:
            p.wait(240)
            note(f"worker {name} exited {p.returncode}")
        except subprocess.TimeoutExpired:
            kill(p, f"worker {name} still running after 240 s")
    done = ledger("SELECT task, COUNT(*) FROM done GROUP BY task")
    runs = ledger("SELECT task, ev, COUNT(*) FROM runs GROUP BY task, ev ORDER BY task, ev")
    status = client("status", o)
    result = (HERE / f"{o}.result.json").read_text(encoding="utf-8") if (HERE / f"{o}.result.json").exists() else ""
    # idle cost and network
    idle = client("idle", "60")
    # pythonw
    pw = spawn("run", "pw-1", "--quick", "--auto", exe=PYW, log="pythonw.log")
    try:
        pw.wait(120)
    except subprocess.TimeoutExpired:
        kill(pw, "pythonw run too slow")
    pw_ok = (HERE / "pw-1.result.json").exists()
    print(json.dumps({"events": events, "done": done, "runs": runs, "status": status, "result": result,
                      "idle": idle, "pythonw_completed": pw_ok}, indent=1))


if __name__ == "__main__":
    main()
