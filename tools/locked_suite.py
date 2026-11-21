"""Run the full test suite under the machine-wide lock (~/.waggle-locks/full-suite.lock, the same one proposals use),
list every failure, then re-run the failures alone once and report which still fail.

    python tools/locked_suite.py            # waits for the lock
Exit 0 when nothing fails on the re-run (load flakes only), 1 when something still fails."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from waggle.core import procs  # noqa: E402

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pytest(args):
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", "--tb=line", *args],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=FLAGS)
    lines = r.stdout.splitlines()
    failed = [l.split(" ", 1)[1].split(" - ")[0] for l in lines if l.startswith("FAILED ")]
    summary = next((l for l in reversed(lines) if " passed" in l or " failed" in l), "")
    return r.returncode, failed, summary


def main():
    lock_path = Path.home() / ".waggle-locks" / "full-suite.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = procs.InstanceLock(lock_path)
    while not lock.acquire():
        time.sleep(30)
    try:
        rc, failed, summary = pytest([])
        print("full suite:", summary)
        if not failed:
            return 0 if rc == 0 else 1
        for f in failed:
            print("  failed:", f)
        rc2, still, summary2 = pytest(failed)
        print("re-run alone:", summary2)
        for f in still:
            print("  STILL FAILS:", f)
        return 1 if still else 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
