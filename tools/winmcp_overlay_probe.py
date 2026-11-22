"""Overlay visibility of Windows-MCP: idle 60 s, one Snapshot, one Click. Run with the windows-mcp venv python.
usage: winmcp_overlay_probe.py OUTDIR [--idle 60] [--cmd python|pythonw] [--server path-to-launcher]"""
import asyncio, ctypes, json, os, subprocess, sys, threading, time
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import psutil
from window_probe import Probe
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

out = Path(sys.argv[1]); out.mkdir(exist_ok=True)
args = sys.argv[2:]
def opt(name, default):
    return args[args.index(name) + 1] if name in args else default
IDLE = float(opt("--idle", "60"))
CMD = opt("--cmd", "python")
SERVER = opt("--server", str(REPO / "tools" / "winmcp_server.py"))
exe = Path(sys.executable).with_name(CMD + ".exe")

u = ctypes.WinDLL("user32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
T0 = time.perf_counter()
vis_log = []   # (t, label, [(hwnd, cls, title, topmost, rect)])
label = ["start"]
stop = threading.Event()

def visible_of(pids):
    found = []
    def cb(h, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(h):
            cls = ctypes.create_unicode_buffer(128); u.GetClassNameW(h, cls, 128)
            ttl = ctypes.create_unicode_buffer(128); u.GetWindowTextW(h, ttl, 128)
            ex = u.GetWindowLongW(h, -20)
            r = wintypes.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            found.append((h, cls.value, ttl.value, bool(ex & 0x8), (r.left, r.top, r.right, r.bottom)))
        return True
    u.EnumWindows(EnumProc(cb), 0)
    return found

def poller():
    me = psutil.Process()
    last = None
    while not stop.is_set():
        pids = {me.pid} | {c.pid for c in me.children(recursive=True)}
        pids.discard(me.pid)
        v = visible_of(pids)
        key = sorted((x[1], x[2], x[3]) for x in v)
        if key != last:
            vis_log.append((round(time.perf_counter() - T0, 3), label[0], [(c, t, tm, r) for _, c, t, tm, r in v]))
            last = key
        time.sleep(0.05)

async def main():
    probe = Probe(root_pid=os.getpid(), keep_all=True, out=str(out / "probe.jsonl"), poll_ms=20)
    probe.start()
    threading.Thread(target=poller, daemon=True).start()
    probe.mark("spawn"); label[0] = "spawn"
    params = StdioServerParameters(command=str(exe), args=[SERVER], env={**os.environ, "ANONYMIZED_TELEMETRY": "false"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            probe.mark("idle"); label[0] = "idle"
            await asyncio.sleep(IDLE)
            probe.mark("snapshot"); label[0] = "snapshot"
            res = await s.call_tool("Snapshot", {})
            text = "\n".join(getattr(c, "text", "") or "" for c in res.content)
            if text.startswith('["'): text = "\n".join(json.loads(text))
            import re
            m = re.search(r'\((-?\d+),(-?\d+)\) document "Text editor', text)
            await asyncio.sleep(3)
            probe.mark("click"); label[0] = "click"
            if m:
                await s.call_tool("Click", {"loc": [int(m.group(1)), int(m.group(2))]})
            else:
                print("no editor found; click skipped")
            await asyncio.sleep(3)
            probe.mark("after"); label[0] = "after"
            probe.mark("lease_end"); label[0] = "lease_end"
            await asyncio.sleep(25)
            probe.mark("shutdown"); label[0] = "shutdown"
    await asyncio.sleep(1)
    stop.set()
    summary = probe.stop()
    json.dump({"summary": summary, "visible": vis_log}, open(out / "result.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))
    for t, lab, v in vis_log:
        print(t, lab, v)

asyncio.run(main())
