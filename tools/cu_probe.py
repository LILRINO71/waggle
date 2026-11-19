"""Probe Hermes computer_use the way the gateway calls it (debug helper).

    <hermes venv python> tools/cu_probe.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
sys.path.insert(0, str(HERMES / "hermes-agent"))
os.chdir(HERMES / "hermes-agent")

from tools.computer_use.tool import handle_computer_use  # noqa: E402


def run(args):
    t0 = time.time()
    out = handle_computer_use(args, task_id="probe", session_id="probe")
    text = out if isinstance(out, str) else json.dumps(out)[:600]
    print(f"{args} -> {time.time() - t0:.1f}s\n   {text[:500]}\n")
    return out


os.environ["HERMES_INTERACTIVE"] = "1"  # the probe stands in for a present user
from tools.approval import load_permanent, _read_permanent_allowlist  # noqa: E402
load_permanent(_read_permanent_allowlist())
subprocess.Popen(["notepad.exe"])
time.sleep(2)
run({"action": "capture", "mode": "ax", "app": "Notepad"})
run({"action": "type", "text": "hello from Waggle", "app": "Notepad"})
