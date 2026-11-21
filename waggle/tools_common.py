"""Shared plumbing for Waggle's real-world tools (tools_cad, tools_docs, tools_media, tools_github, tools_explain).

The rules every one of those tools keeps (decided here once, tested in tests/test_tools_common.py):
- inputs: an existing file the user could read, never a secrets file (policy.is_secret_path);
- outputs: always a NEW file inside the folders Waggle may write (policy.write_roots), never an overwrite: a name
  that is taken gets " (2)", " (3)"... ; with no `out` the file lands next to the source when that folder is
  writable for Waggle, else in <jobs_dir>/tool-output;
- every child process is started without a console window (the core runs under pythonw);
- a job that overruns its timeout is killed with its children, and the answer says so;
- tools answer in JSON: {"ok": true, ...} or {"ok": false, "error": "...", "hint": "..."} - never a traceback.
"""
import json
import os
import subprocess
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)  # heavy jobs must not slow the owner's PC


class ToolError(Exception):
    """A failure the caller should read: message says what happened, hint says what to try."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


def ok(**kw):
    return json.dumps({"ok": True, **kw}, default=str, ensure_ascii=False)


def fail(message, hint=""):
    out = {"ok": False, "error": str(message)}
    if hint:
        out["hint"] = hint
    return json.dumps(out, ensure_ascii=False)


def guarded(fn):
    """Wrap a tool function: ToolError and any other exception become a JSON failure."""
    def run(args, **kw):
        try:
            return fn(args or {}, **kw)
        except ToolError as e:
            return fail(e, e.hint)
        except subprocess.TimeoutExpired as e:
            return fail(f"timed out after {e.timeout:.0f} s", "try a smaller input")
        except Exception as e:  # a tool must never raise into the chat brain
            return fail(f"{type(e).__name__}: {e}")
    run.__name__ = getattr(fn, "__name__", "tool")
    return run


# ---- paths -----------------------------------------------------------------------------------------

def _policy():
    from . import policy
    return policy


def read_path(path, exts=None):
    """The input file: absolute, existing, not a secrets file, optionally one of `exts` (lowercase, with dot)."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise ToolError("no input path given")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path.home() / p
    if not p.exists():
        raise ToolError(f"not found: {p}", "give the full path of an existing file")
    if _policy().is_secret_path(str(p)):
        raise ToolError("that file looks like a secrets file; Waggle's tools do not read those")
    if exts is not None and p.is_file() and p.suffix.lower() not in exts:
        raise ToolError(f"{p.suffix or 'that file'} is not supported here (supported: {', '.join(sorted(exts))})")
    return p


def writable_roots(settings=None):
    return _policy().write_roots(settings)


def default_out_dir(src, settings=None):
    """Next to the source when Waggle may write there, else <jobs_dir>/tool-output."""
    from .config import Settings
    s = settings or Settings.load()
    pol = _policy()
    if pol.under(str(src.parent), writable_roots(s)):
        return src.parent
    return Path(s.jobs_dir) / "tool-output"


def free_name(path):
    """`path`, or `name (2).ext`, `name (3).ext`... if it is taken. Existing files are never overwritten."""
    path = Path(path)
    if not path.exists():
        return path
    for n in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
    raise ToolError("could not find a free file name")


def out_path(src, ext, out=None, settings=None, suffix=""):
    """Where a result goes: `out` if given (must be writable for Waggle), else next to the source (see module doc)."""
    from .config import Settings
    s = settings or Settings.load()
    if out:
        p = Path(os.path.expanduser(str(out).strip().strip('"')))
        if not p.is_absolute():
            p = default_out_dir(src, s) / p
        if p.suffix.lower() != ext.lower():
            p = p.with_suffix(ext)
        if not _policy().under(str(p.parent), writable_roots(s)):
            raise ToolError(f"Waggle may not write to {p.parent}",
                            f"leave `out` empty (the file goes to {default_out_dir(src, s)}) or use a folder under "
                            f"{Path.home() / 'Claude'}")
        if _policy().is_secret_path(str(p)):
            raise ToolError("that output path looks like a secrets location")
    else:
        p = default_out_dir(src, s) / (src.stem + suffix + ext)
    p.parent.mkdir(parents=True, exist_ok=True)
    return free_name(p)


# ---- processes -------------------------------------------------------------------------------------

def run(cmd, timeout=120, cwd=None, env=None, stdin=None, low_priority=False):
    """Run a child without a window; returns (returncode, stdout, stderr). On timeout the whole tree is killed and
    subprocess.TimeoutExpired is raised (guarded() turns that into a plain failure)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL if stdin is None
                         else subprocess.PIPE, cwd=cwd, env=env,
                         creationflags=NO_WINDOW | (BELOW_NORMAL if low_priority else 0))
    try:
        out, err = p.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)
        p.kill()
        p.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def kill_tree(pid):
    """Kill a process and its children (taskkill /T; best effort)."""
    if os.name != "nt":
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except Exception:
        pass


def which(*names, extra=()):
    """First existing executable: PATH lookup of each name, then the `extra` absolute paths (globs allowed)."""
    import glob
    import shutil
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
    for pattern in extra:
        for hit in sorted(glob.glob(os.path.expandvars(str(pattern))), reverse=True):
            if os.path.isfile(hit):
                return hit
    return ""


def tail(text, n=400):
    text = (text or "").strip()
    return text[-n:]


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def file_info(p):
    p = Path(p)
    return {"path": str(p), "bytes": p.stat().st_size, "size": human_bytes(p.stat().st_size)}


def now():
    return time.time()
