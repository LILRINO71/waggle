"""Windows-MCP (CursorTouch/Windows-MCP, MIT) as an MCP server for the chat brain: it reads the Windows UI Automation
tree and acts on elements by label, which is exact where pixel clicking was not (a Google Doc took 7 clicks/keys).

Installed on its own, outside Waggle's interpreter:
    py -3.14 -m venv %USERPROFILE%\\.waggle-tools\\windows-mcp\\venv
    %USERPROFILE%\\.waggle-tools\\windows-mcp\\venv\\Scripts\\python -m pip install windows-mcp==0.8.8
0.8.8 is the newest release older than two weeks (0.8.9 was 9 days old on 2026-11-21). Every call also passes the
policy hook (waggle.security.winmcp_check): blocked apps, secrets, payment pages and the Claude window.
"""
from pathlib import Path

from .config import data_path

VERSION = "0.8.8"
ROOT = Path.home() / ".waggle-tools" / "windows-mcp"
LAUNCHER = Path(__file__).resolve().parent.parent / "tools" / "winmcp_server.py"
SERVER_NAME = "windows-mcp"  # tool names become mcp__windows-mcp__Click ...


def python():
    return ROOT / "venv" / "Scripts" / "python.exe"


def windowless_python():
    """pythonw.exe next to python.exe: started by a process with no console (the chat brain), python.exe opens a console
    window and takes the focus (measured with tools/window_probe.py); pythonw.exe opens nothing."""
    w = python().with_name("pythonw.exe")
    return w if w.exists() else python()


def installed():
    return python().exists() and LAUNCHER.exists()


def server_entry():
    """The MCP config entry: the venv's python running the launcher (which fixes the tool list)."""
    return {"type": "stdio", "command": str(windowless_python()), "args": [str(LAUNCHER)],
            "env": {"ANONYMIZED_TELEMETRY": "false", "WINDOWS_MCP_DISABLE_FLASH": "1", "PYTHONUTF8": "1",
                    "WAGGLE_HOME": str(data_path("."))}}
