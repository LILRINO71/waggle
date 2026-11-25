"""Live progress of a running agent, so "how is it going?" has a real answer while the work runs.

An agent run (waggle/agent.py) feeds each stream-json line it reads to a Tracker. The tracker keeps a small JSON
file per run under <data>/progress/: when it started, when it last did something, how many tool steps so far, the
last few steps in one short line each, and the files it wrote or edited. background_status and objective_status
read it (read()). Found by the 2026-11-25 real-project tests: an 11-minute build answered "not yet" three times,
and the money objective said "no results yet" while 20 fetched pages already sat in its folder.
"""
import json
import re
import time
from pathlib import Path

from .config import data_path

RECENT = 5
FILES = 10
KEEP_DAYS = 3
WRITERS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _file(key):
    return data_path("progress") / (re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))[:120] + ".json")


def brief(tool, inp):
    """One short line for a tool step: what it touched, never its full input."""
    inp = inp if isinstance(inp, dict) else {}
    target = (inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("url")
              or inp.get("command") or inp.get("query") or inp.get("pattern") or inp.get("description") or "")
    target = " ".join(str(target).split())
    name = str(tool).replace("mcp__waggle__", "")
    return f"{name}: {target[:90]}" if target else name


class Tracker:
    def __init__(self, key, now=None):
        self.path = _file(key)
        self.state = {"key": str(key), "started": now or time.time(), "updated": now or time.time(), "steps": 0,
                      "recent": [], "files": [], "finished": None}
        self._save()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # progress is a nicety: it never breaks a run

    def feed(self, line, now=None):
        line = str(line).strip()
        if not line.startswith("{") or '"tool_use"' not in line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if ev.get("type") != "assistant":
            return
        changed = False
        for block in (ev.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input") or {}
            self.state["steps"] += 1
            self.state["recent"] = (self.state["recent"] + [brief(block.get("name", ""), inp)])[-RECENT:]
            f = isinstance(inp, dict) and (inp.get("file_path") or inp.get("notebook_path"))
            if block.get("name") in WRITERS and f and f not in self.state["files"]:
                self.state["files"] = (self.state["files"] + [str(f)])[-FILES:]
            changed = True
        if changed:
            self.state["updated"] = now or time.time()
            self._save()

    def finish(self, now=None):
        self.state["finished"] = now or time.time()
        self._save()


def read(key, now=None):
    """The progress of run `key` for a status answer, or None when there is none: minutes running, minutes since
    its last step, step count, the last steps and the files it wrote."""
    try:
        st = json.loads(_file(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    now = now or time.time()
    end = st.get("finished") or now
    return {"running_min": round((end - st["started"]) / 60, 1),
            "last_step_min_ago": round((now - st["updated"]) / 60, 1),
            "steps": st.get("steps", 0), "recent_steps": st.get("recent", []), "files_written": st.get("files", []),
            "finished": bool(st.get("finished"))}


def prune(now=None):
    now = now or time.time()
    root = data_path("progress")
    gone = 0
    for p in root.glob("*.json") if root.exists() else []:
        try:
            if now - p.stat().st_mtime > KEEP_DAYS * 86400:
                p.unlink()
                gone += 1
        except OSError:
            pass
    return gone
