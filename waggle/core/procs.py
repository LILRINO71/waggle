"""Is a recorded process still the same process? A pid alone is not enough: Windows reuses pids, so a job also
records the process creation time and a live pid with a different creation time counts as dead."""
import os

_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000
_ACCESS_DENIED = 5

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _probe(pid):
        """(alive, creation time in 100 ns ticks since 1601, or None if it cannot be read)."""
        h = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not h:
            # A process we may not query still exists; anything else (invalid parameter) means no such process.
            return (ctypes.get_last_error() == _ACCESS_DENIED), None
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False, None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
                return True, None
            created = times[0]
            return True, (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            _k32.CloseHandle(h)
else:
    def _probe(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, None
        except PermissionError:
            return True, None
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii") as f:
                return True, int(f.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return True, None


def alive(pid):
    if not pid or int(pid) <= 0:
        return False
    return _probe(int(pid))[0]


def ctime(pid):
    """Creation time of a running process as an opaque integer, or None if it is not running or unreadable."""
    if not pid or int(pid) <= 0:
        return None
    is_alive, created = _probe(int(pid))
    return created if is_alive else None


def same_process(pid, pid_ctime):
    """True when `pid` runs and is the process that had `pid_ctime` (unknown creation times are trusted)."""
    if not pid or int(pid) <= 0:
        return False
    is_alive, created = _probe(int(pid))
    if not is_alive:
        return False
    return pid_ctime is None or created is None or int(created) == int(pid_ctime)


_own = None


def own_ctime():
    global _own
    if _own is None or _own[0] != os.getpid():
        _own = (os.getpid(), ctime(os.getpid()))
    return _own[1]


# ---- starting and stopping processes -------------------------------------------------------------------------

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NEW_CONSOLE = 0x00000010
# Console tools started from the windowless core (git, taskkill, schtasks, claude) must not flash a window: a
# console program started by a process without a console gets its own, and with Windows Terminal as the default
# terminal that is a terminal window that also takes the focus (measured: ~80 windows per outreach.look pass).
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# Windows shows the "working in background" cursor for up to 2 s when a GUI program (pythonw) starts, unless the
# creator forces feedback off (measured: one blink per detached job worker started by the core).
STARTF_FORCEOFFFEEDBACK = 0x80


def quiet_flags(flags=0):
    """`flags` plus CREATE_NO_WINDOW, unless the caller chose how the child gets its console (no console at all,
    or a new visible one on purpose)."""
    if os.name != "nt":
        return flags
    if flags & (NO_WINDOW | _DETACHED_PROCESS | _CREATE_NEW_CONSOLE):
        return flags
    return flags | NO_WINDOW


def quiet_startupinfo(info=None):
    """A STARTUPINFO (a copy of `info` when given) that forces the app-starting cursor off; None off Windows."""
    if os.name != "nt":
        return info
    import copy
    import subprocess
    out = copy.copy(info) if info is not None else subprocess.STARTUPINFO()
    out.dwFlags |= STARTF_FORCEOFFFEEDBACK
    return out


_original_popen_init = None


def quiet_installed():
    return _original_popen_init is not None


def install_quiet_children():
    """Make every child this process starts quiet by default: subprocess.Popen (and so run/check_output/...) adds
    CREATE_NO_WINDOW and STARTF_FORCEOFFFEEDBACK unless the caller chose its own console flags. The core and its
    detached workers call this at start, so code in a plug-in that forgot the flags cannot flash a window.
    Returns True when it installed the wrapper now (False off Windows or when already installed)."""
    global _original_popen_init
    if os.name != "nt" or _original_popen_init is not None:
        return False
    import subprocess
    original = subprocess.Popen.__init__

    def quiet_init(self, *args, **kwargs):
        # `startupinfo` and `creationflags` are keyword arguments in all of Waggle's calls; a call that passes
        # them positionally (14+ positional arguments) is left alone.
        if len(args) < 13:
            kwargs["creationflags"] = quiet_flags(kwargs.get("creationflags") or 0)
            kwargs["startupinfo"] = quiet_startupinfo(kwargs.get("startupinfo"))
        original(self, *args, **kwargs)

    quiet_init.__wrapped__ = original
    _original_popen_init = original
    subprocess.Popen.__init__ = quiet_init
    return True


def uninstall_quiet_children():
    """Undo install_quiet_children (tests)."""
    global _original_popen_init
    if _original_popen_init is None:
        return
    import subprocess
    subprocess.Popen.__init__ = _original_popen_init
    _original_popen_init = None


def pythonw():
    """The windowless interpreter next to this one when there is one (no console window pops up)."""
    import sys
    from pathlib import Path
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def spawn_detached(args, *, cwd, log_path, env=None):
    """Start a process that outlives its parent (also when the parent runs inside a job object that kills its
    members on close, as Task Scheduler tasks can). Output goes to `log_path`. Returns the pid. On Windows the
    child has no console (DETACHED_PROCESS) and the app-starting cursor is forced off."""
    import subprocess
    log = open(log_path, "ab")
    try:
        kw = dict(cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  close_fds=True)
        if os.name != "nt":
            return subprocess.Popen(args, start_new_session=True, **kw).pid  # window: ok - not Windows
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        kw["startupinfo"] = quiet_startupinfo()
        try:
            return subprocess.Popen(args, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kw).pid
        except OSError:  # the surrounding job forbids breakaway
            return subprocess.Popen(args, creationflags=flags, **kw).pid
    finally:
        log.close()


def kill_tree(pid, pid_ctime=None):
    """Kill a process and its children, but only if `pid` is still the recorded process. True if killed."""
    import subprocess
    if not same_process(pid, pid_ctime):
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, creationflags=NO_WINDOW,
                       startupinfo=quiet_startupinfo())
    else:
        import signal
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        except OSError:
            os.kill(int(pid), signal.SIGKILL)
    return True


class InstanceLock:
    """Holds a byte-range lock on a file for the life of the process (released by the OS if it dies)."""

    def __init__(self, path):
        self.path = path
        self._f = None

    def acquire(self):
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self):
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None


def lock_held(path):
    """Is another process holding the lock at `path`? (Probes by taking and releasing it at once.)"""
    probe = InstanceLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True
