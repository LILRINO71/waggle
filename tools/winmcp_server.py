"""Starts Windows-MCP (pinned, in ~/.waggle-tools/windows-mcp/venv) for the chat brain. Run with that venv's python.

What this adds to a plain `windows-mcp serve`:
  - telemetry off and no flash border, whatever the caller's environment says;
  - only the tools Waggle offers (no PowerShell, Registry, FileSystem, Process, Scrape, Move...);
  - the server's own overlay windows left out of window enumeration: reading them from inside this process cost 6 s
    per Snapshot (22 s -> about 2 s with the 500-element tree cap);
  - after every Snapshot, a label -> window map in <WAGGLE_HOME>/winmcp_state.json. Click and Type take a label, not a
    window name, so waggle.security.winmcp_check reads this file to know which window a label belongs to.
"""
import json
import os
import sys
import time
from pathlib import Path

TOOLS = "App,Snapshot,WaitFor,Wait,Click,Type,Scroll,Shortcut,Clipboard"


def state_path():
    return Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle") / "winmcp_state.json"


def label_map(desktop):
    """{label: {window, name, type, x, y}} in the order Windows-MCP numbers them (interactive, then scrollable)."""
    tree = desktop.desktop_state.tree_state
    out = {}
    for i, n in enumerate(list(tree.interactive_nodes) + list(tree.scrollable_nodes)):
        out[str(i)] = {"window": n.window_name, "name": n.name[:80], "type": n.control_type,
                       "x": n.center.x, "y": n.center.y}
    active = desktop.desktop_state.active_window
    return {"t": time.time(), "active": getattr(active, "name", ""), "labels": out}


def dump(desktop):
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(label_map(desktop)), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # no map = every label click asks (waggle.security), never a silent pass
        try:
            p.unlink()
        except OSError:
            pass


def _pid(win32process, hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return 0


def main():
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["WINDOWS_MCP_DISABLE_FLASH"] = "1"
    from windows_mcp.desktop.service import Desktop
    real = Desktop.get_state

    def get_state(self, *a, **k):
        out = real(self, *a, **k)
        dump(self)
        return out

    Desktop.get_state = get_state
    real_handles = Desktop.get_controls_handles

    def get_controls_handles(self, *a, **k):
        import win32process
        me = os.getpid()
        return {h for h in real_handles(self, *a, **k) if _pid(win32process, h) != me}

    Desktop.get_controls_handles = get_controls_handles
    from windows_mcp.__main__ import main as serve
    sys.argv = ["windows-mcp", "serve", "--tools", TOOLS]
    serve()


if __name__ == "__main__":
    main()
