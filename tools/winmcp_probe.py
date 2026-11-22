"""Throwaway live proof of Windows-MCP, no Waggle process involved. Run with the Windows-MCP venv's python:

    %USERPROFILE%\\.waggle-tools\\windows-mcp\\venv\\Scripts\\python tools\\winmcp_probe.py [--app-launch]

Opens Notepad, types "Waggle test", reads the UI tree to confirm the text, closes Notepad choosing "Don't save", and
prints the time of every step. Each acting call is first run through waggle.security.winmcp_check, the same check the
policy hook makes. Only the Notepad process this script started (found by its new PID) is ever stopped, and only if the
polite close failed.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("WAGGLE_HOME", str(Path.home() / ".waggle"))
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from waggle import security  # noqa: E402

TIMES = []


def notepad_pids():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-Process notepad -ErrorAction SilentlyContinue).Id -join ','"],
                         capture_output=True, text=True).stdout.strip()
    return {int(x) for x in out.split(",") if x}


def text_of(result):
    text = "\n".join(getattr(c, "text", "") or "" for c in result.content)
    if text.startswith('["'):  # Snapshot comes back as a JSON list of strings
        try:
            return "\n".join(json.loads(text))
        except ValueError:
            pass
    return text


def find_loc(snapshot, role, name):
    """[x, y] of the first '(x,y) role "name"' row of a Snapshot's UI tree (0.8.x lists points, not label numbers)."""
    m = re.search(r'\((-?\d+),(-?\d+)\) ' + re.escape(role) + r' "' + re.escape(name), snapshot)
    return [int(m.group(1)), int(m.group(2))] if m else None


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(REPO / "tools" / "winmcp_server.py")],
                                   env={**os.environ, "ANONYMIZED_TELEMETRY": "false"})
    t0 = time.perf_counter()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            TIMES.append(("start server + initialize", time.perf_counter() - t0))

            async def call(tool, **args):
                full = f"mcp__windows-mcp__{tool}"
                verdict = security.winmcp_check(full, args)
                if verdict:
                    raise SystemExit(f"guard said {verdict} for {tool} {args}")
                t = time.perf_counter()
                res = await s.call_tool(tool, args)
                TIMES.append((f"{tool} {args if tool != 'Snapshot' else ''}".strip(), time.perf_counter() - t))
                return text_of(res)

            tools = [t.name for t in (await s.list_tools()).tools]
            print("tools offered:", tools)
            before = notepad_pids()
            if "--app-launch" in sys.argv:  # Windows-MCP's own launcher: it lists the Start Menu first, about 11 s
                print(await call("App", mode="launch", name="notepad"))
            else:  # what the brain is told to do: Start-Process from Waggle's shell, then act through Windows-MCP
                t = time.perf_counter()
                subprocess.run(["powershell", "-NoProfile", "-Command", "Start-Process notepad.exe"])
                TIMES.append(("Start-Process notepad.exe (shell)", time.perf_counter() - t))
            await call("Wait", duration=1)
            await call("Shortcut", shortcut="ctrl+n")  # Notepad restores old tabs: type in a fresh one
            await call("Wait", duration=1)
            snap = await call("Snapshot")
            Path(os.environ.get("WINMCP_DUMP", "winmcp_snapshot1.txt")).write_text(snap, encoding="utf-8")
            editor = find_loc(snap, "document", "Text editor")
            print("editor at:", editor)
            print(await call("Type", loc=editor, text="Waggle test"))
            snap2 = await call("Snapshot")
            ok = bool(re.search(r'document "Text editor".*\[value:"Waggle test"\]', snap2))
            print("text visible in UI tree:", ok)
            started = notepad_pids() - before
            await call("Shortcut", shortcut="ctrl+w")  # close the new tab: Notepad asks to save
            snap3 = await call("Snapshot")
            dont = find_loc(snap3, "button", "Don't save")
            print("Don't save at:", dont)
            if dont:
                print(await call("Click", loc=dont))
            await call("Shortcut", shortcut="alt+f4")  # close Notepad (the old tab stays in its saved session)
            await asyncio.sleep(1)
            left = notepad_pids() & started
            for pid in left:  # polite close failed: stop only the process this script started
                subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"])
            print("notepad left behind:", sorted(left), "| text confirmed:", ok)
    for name, sec in TIMES:
        print(f"{sec * 1000:8.0f} ms  {name}")


if __name__ == "__main__":
    asyncio.run(main())
