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
