"""window_probe: measures what a background program does to the user's screen (Windows, stdlib + ctypes only).

While it runs it records, as JSON lines with timestamps:
  window   a top-level window that became visible (class, title, pid, process name, parent chain)
  focus    a change of the foreground window (new and previous window, owner process)
  cursor   the mouse cursor changing to or from the "app starting" (working in background) cursor
  proc     a new process whose ancestry matches the filter (so a flash can be tied to a spawn)
Windows and focus come from WinEvent hooks (EVENT_OBJECT_SHOW / CREATE, EVENT_SYSTEM_FOREGROUND) plus a
poll over EnumWindows as a cross-check; the cursor and the process table are polled (default every 20 ms).

An event is "related" when its process, or an ancestor of it, descends from --root-pid, or is named in --names
(optionally only if that command line contains --cmd-contains). A console host or terminal window whose
owner could not be resolved counts as related when a related process started in the 600 ms before it
("recent_spawn"). Unrelated events are only counted unless --all is given.

  python tools/window_probe.py --out probe.jsonl --duration 60 --root-pid 1234
  python tools/window_probe.py --out probe.jsonl --names python,pythonw,claude --cmd-contains waggle --duration 120
  python tools/window_probe.py --out probe.jsonl --run pythonw -m waggle.core watch

As a library: p = Probe(root_pid=os.getpid()); p.start(); p.mark("scenario"); ...; p.stop(); p.summary()."""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

DEFAULT_NAMES = ["python", "pythonw", "claude", "node", "git", "gh", "bash", "sh", "conhost", "cmd", "powershell",
                 "schtasks", "taskkill", "openconsole", "windowsterminal"]
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}
CONSOLE_HOSTS = {"conhost", "openconsole", "windowsterminal"}
SPAWN_WINDOW_S = 0.6

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_SHOW = 0x8002
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0
WINEVENT_SKIPOWNPROCESS = 2
IDC_APPSTARTING = 32650
TH32CS_SNAPPROCESS = 2
GA_ROOT = 2
GWL_EXSTYLE = -20
PM_REMOVE = 1
PROCESS_QUERY_LIMITED = 0x1000
PROCESS_QUERY_VM_READ = 0x0410


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", wintypes.POINT)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", ctypes.c_void_p)]


class PBI(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


if os.name == "nt":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    HOOKPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.LONG,
                                  wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)

    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
    k32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t))
    nt.NtQueryInformationProcess.argtypes = (ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                             ctypes.c_void_p)
    u32.SetWinEventHook.restype = ctypes.c_void_p
    u32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, HOOKPROC, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD)
    u32.UnhookWinEvent.argtypes = (ctypes.c_void_p,)
    u32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
    u32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
    u32.GetAncestor.restype = ctypes.c_void_p
    u32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
    u32.GetWindow.restype = ctypes.c_void_p
    u32.GetWindow.argtypes = (ctypes.c_void_p, wintypes.UINT)
    u32.GetClassNameW.argtypes = (ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int)
    u32.GetWindowTextW.argtypes = (ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int)
    u32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.RECT))
    u32.GetWindowLongW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    u32.GetCursorInfo.argtypes = (ctypes.POINTER(CURSORINFO),)
    u32.LoadCursorW.restype = ctypes.c_void_p
    u32.LoadCursorW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u32.DefWindowProcW.restype = ctypes.c_ssize_t
    u32.DefWindowProcW.argtypes = (ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
    u32.CreateWindowExW.restype = ctypes.c_void_p
    u32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p)
    u32.DestroyWindow.argtypes = (ctypes.c_void_p,)
    u32.SetLayeredWindowAttributes.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD)
    u32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    u32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    u32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    u32.EnumWindows.argtypes = (ENUMPROC, wintypes.LPARAM)
    u32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT)
    u32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    u32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)

WS_EX_TOOLWINDOW = 0x80


def _hex(h):
    return f"0x{(h or 0):x}"


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _cmdline(pid):
    h = k32.OpenProcess(PROCESS_QUERY_VM_READ, False, pid)
    if not h:
        return None
    try:
        pbi = PBI()
        if nt.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0 \
                or not pbi.PebBaseAddress:
            return None
        ptr = ctypes.c_void_p()
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(pbi.PebBaseAddress + 0x20), ctypes.byref(ptr), 8,
                                     ctypes.byref(n)) or not ptr.value:
            return None
        us = (ctypes.c_byte * 16)()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(ptr.value + 0x70), us, 16, ctypes.byref(n)):
            return None
        raw = bytes(us)
        length = int.from_bytes(raw[0:2], "little")
        buf = int.from_bytes(raw[8:16], "little")
        if not length or not buf or length > 65000:
            return None
        data = (ctypes.c_byte * length)()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(buf), data, length, ctypes.byref(n)):
            return None
        return bytes(data).decode("utf-16-le", "replace")
    except Exception:
        return None
    finally:
        k32.CloseHandle(h)


def _ctime(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return _ft(t[0])
    finally:
        k32.CloseHandle(h)


class ProcTable:
    """Process table refreshed from a Toolhelp snapshot; keeps dead processes so late events can be explained."""

    def __init__(self):
        self.info = {}      # pid -> dict(ppid, name, ctime, cmd, seen)
        self.live = set()

    def snapshot(self):
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        rows = {}
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                rows[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return rows

    def refresh(self, now):
        """Returns the list of pids that appeared since the last refresh."""
        rows = self.snapshot()
        new = []
        for pid, (ppid, exe) in rows.items():
            old = self.info.get(pid)
            name = exe[:-4].lower() if exe.lower().endswith(".exe") else exe.lower()
            if old is None or old["ppid"] != ppid or old["name"] != name or pid not in self.live:
                if old is not None and pid in self.live and old["ppid"] == ppid and old["name"] == name:
                    continue
                created = _ctime(pid)
                if old is not None and old["ppid"] == ppid and old["name"] == name and old["ctime"] == created:
                    continue
                self.info[pid] = {"ppid": ppid, "name": name, "ctime": created, "cmd": _cmdline(pid), "seen": now}
                new.append(pid)
        self.live = set(rows)
        return new

    def chain(self, pid, limit=12):
        out, seen = [], set()
        cur = pid
        while cur and cur in self.info and cur not in seen and len(out) < limit:
            seen.add(cur)
            rec = self.info[cur]
            out.append(cur)
            parent = rec["ppid"]
            prec = self.info.get(parent)
            # a recycled parent pid is a different, younger process
            if prec is None or (prec["ctime"] and rec["ctime"] and prec["ctime"] > rec["ctime"]):
                break
            cur = parent
        return out

    def describe(self, pid):
        rec = self.info.get(pid)
        if rec is None:
            return {"pid": pid, "name": None}
        return {"pid": pid, "name": rec["name"], "cmd": (rec["cmd"] or "")[:160] or None}


class Probe:
    def __init__(self, root_pid=None, names=None, cmd_contains=None, poll_ms=20, out=None, keep_all=False,
                 skip_own=True, park_cursor=False):
        self.root_pid = root_pid
        self.names = {n.lower().removesuffix(".exe") for n in (names or [])}
        self.cmd_contains = [c.lower() for c in (cmd_contains or [])]
        self.poll_s = max(0.002, poll_ms / 1000.0)
        self.keep_all = keep_all
        self.skip_own = skip_own
        self.park_cursor = park_cursor
        self._sensor = None
        self._restore_pos = None
        self.reparks = 0           # times the mouse was found off the sensor and put back
        self._next_repark = 0.0
        self.out = open(out, "w", encoding="utf-8") if out else None
        self.events = []
        self.counts = {}  # label -> metric counts
        self.label = ""
        self._t0 = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.table = ProcTable()
        self._recent = []          # (time, name, pid) of related process starts
        self._windows_seen = {}    # hwnd -> time reported
        self._fg = None
        self._appstart = None      # dict while the app-starting cursor shows
        self.error = None

    # ---- relation ----------------------------------------------------------------------------------------

    def _matches_name(self, pid):
        rec = self.table.info.get(pid)
        if rec is None or rec["name"] not in self.names:
            return False
        if self.cmd_contains:
            cmd = (rec["cmd"] or "").lower()
            return any(c in cmd for c in self.cmd_contains)
        return True

    def related_by(self, pid):
        """'root' / 'name:<n>' / None for the process and its ancestors."""
        if not pid or pid == os.getpid():
            return None
        chain = self.table.chain(pid)
        if self.root_pid and self.root_pid in chain:
            return "root"
        for p in chain:
            if self._matches_name(p):
                return "name:" + self.table.info[p]["name"]
        return None

    def _recent_spawns(self, now):
        return [f"{n}:{p}" for (t, n, p) in self._recent if now - t <= SPAWN_WINDOW_S]

    # ---- recording ---------------------------------------------------------------------------------------

    def mark(self, label):
        self.label = label
        self._write({"ev": "mark", "label": label})

    METRICS = ("windows", "focus", "appstarting", "procs", "unrelated_windows", "unrelated_focus",
               "unrelated_appstarting")

    def _count(self, rec, related):
        ev = rec["ev"]
        if ev == "cursor" and rec.get("state") != "appstarting_on":
            return
        metric = {"window": "windows", "focus": "focus", "cursor": "appstarting", "proc": "procs"}.get(ev)
        if metric is None:
            return
        if not related and metric != "procs":
            metric = "unrelated_" + metric
        row = self.counts.setdefault(rec["label"], dict.fromkeys(self.METRICS, 0))
        row[metric] += 1

    def _write(self, rec, related=True):
        now = time.time()
        rec = {"t": round(now - (self._t0 or now), 3), "wall": round(now, 3), "label": self.label, **rec}
        with self._lock:
            self._count(rec, bool(rec.get("related", related)))
            if related or self.keep_all or rec["ev"] == "mark":
                self.events.append(rec)
                if self.out:
                    self.out.write(json.dumps(rec, default=str) + "\n")
                    self.out.flush()

    # ---- window facts ------------------------------------------------------------------------------------

    @staticmethod
    def _text(fn, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        n = fn(hwnd, buf, 256)
        return buf.value if n else ""

    def _window_info(self, hwnd):
        if hwnd is None:
            return None
        pid = wintypes.DWORD()
        tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = self._text(u32.GetClassNameW, hwnd)
        info = {"hwnd": _hex(hwnd), "class": cls or None, "title": self._text(u32.GetWindowTextW, hwnd)[:100],
                "pid": pid.value or None, "tid": tid or None}
        if not tid:
            info["gone"] = True
        rect = wintypes.RECT()
        if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            info["rect"] = [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]
        if pid.value:
            chain = self.table.chain(pid.value)
            info["process"] = self.table.info.get(pid.value, {}).get("name")
            info["chain"] = [f"{self.table.info[p]['name']}:{p}" for p in chain]
            info["cmd"] = (self.table.info.get(pid.value, {}).get("cmd") or "")[:160] or None
        return info

    def _relate_window(self, info):
        """(related_by, spawns) for a window described by _window_info."""
        by = self.related_by(info.get("pid")) if info.get("pid") else None
        spawns = self._recent_spawns(time.time())
        if by is None and spawns:
            owner = (info.get("process") or "")
            if info.get("class") in CONSOLE_CLASSES or owner in CONSOLE_HOSTS or info.get("gone") \
                    or not info.get("pid"):
                by = "recent_spawn"
        return by, spawns

    def _toplevel_visible(self, hwnd):
        return bool(hwnd) and bool(u32.IsWindowVisible(hwnd)) and u32.GetAncestor(hwnd, GA_ROOT) == hwnd

    def _on_window(self, hwnd, source):
        if not self._toplevel_visible(hwnd):
            return
        prev = self._windows_seen.get(hwnd)
        now = time.time()
        if prev is not None and now - prev < 1.0:
            return
        self._windows_seen[hwnd] = now
        info = self._window_info(hwnd)
        if info is None:
            return
        if self.skip_own and info.get("pid") == os.getpid():
            return
        ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        info["toolwindow"] = bool(ex & WS_EX_TOOLWINDOW)
        by, spawns = self._relate_window(info)
        self._write({"ev": "window", "source": source, **info, "related": by is not None, "related_by": by,
                     "spawns": spawns}, related=by is not None)

    def _on_foreground(self, hwnd):
        prev = self._fg
        self._fg = hwnd
        if hwnd == prev:
            return
        info = self._window_info(hwnd) or {"hwnd": _hex(hwnd)}
        by, spawns = self._relate_window(info) if hwnd else (None, [])
        rec = {"ev": "focus", **info, "related": by is not None, "related_by": by, "spawns": spawns}
        if prev:
            p = self._window_info(prev) or {}
            rec["prev"] = {k: p.get(k) for k in ("hwnd", "class", "title", "process", "pid")}
        self._write(rec, related=by is not None)

    def _poll_cursor(self, app_handle):
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(ci)
        if not u32.GetCursorInfo(ctypes.byref(ci)):
            return
        now = time.time()
        showing = bool(ci.hCursor) and ci.hCursor == app_handle
        if showing and self._appstart is None:
            spawns = self._recent_spawns(now)
            self._appstart = {"since": now, "spawns": spawns}
            self._write({"ev": "cursor", "state": "appstarting_on", "handle": _hex(ci.hCursor),
                         "pos": [ci.ptScreenPos.x, ci.ptScreenPos.y], "spawns": spawns,
                         "related": bool(spawns), "related_by": "recent_spawn" if spawns else None},
                        related=bool(spawns))
        elif not showing and self._appstart is not None:
            began = self._appstart
            self._appstart = None
            self._write({"ev": "cursor", "state": "appstarting_off", "duration_s": round(now - began["since"], 3),
                         "spawns": began["spawns"], "related": bool(began["spawns"]),
                         "related_by": "recent_spawn" if began["spawns"] else None},
                        related=bool(began["spawns"]))

    # ---- the thread --------------------------------------------------------------------------------------

    def _enum_visible(self):
        found = []

        def cb(hwnd, _):
            if u32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        u32.EnumWindows(ENUMPROC(cb), 0)
        return found

    def _make_sensor(self):
        """A nearly invisible window of the probe's own whose class cursor is the arrow, with the mouse parked
        on it. The app-starting cursor is only drawn over a window that leaves the cursor to the system, so
        without this a cursor over an editor or a terminal would hide the effect being measured."""
        keep = WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(cls)
        cls.lpfnWndProc = ctypes.cast(keep, ctypes.c_void_p)
        cls.hInstance = k32.GetModuleHandleW(None)
        cls.hCursor = u32.LoadCursorW(None, 32512)
        cls.lpszClassName = "WindowProbeSensor"
        u32.RegisterClassExW(ctypes.byref(cls))
        w, h = 48, 48
        x = u32.GetSystemMetrics(0) - w - 24
        y = u32.GetSystemMetrics(1) - h - 120
        # WS_POPUP | WS_VISIBLE ; TOPMOST | LAYERED | TOOLWINDOW | NOACTIVATE
        hwnd = u32.CreateWindowExW(0x8 | 0x80000 | 0x80 | 0x8000000, cls.lpszClassName, "", 0x80000000 | 0x10000000,
                                   x, y, w, h, None, None, cls.hInstance, None)
        if not hwnd:
            return None
        u32.SetLayeredWindowAttributes(hwnd, 0, 3, 2)
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        self._restore_pos = (pt.x, pt.y)
        u32.SetCursorPos(x + w // 2, y + h // 2)
        self._sensor = (hwnd, keep, (x + w // 2, y + h // 2))
        return hwnd

    def _repark(self, now):
        """Keep the mouse on the sensor (the user moving it would blind the busy-cursor measurement)."""
        if not self._sensor or now < self._next_repark:
            return
        self._next_repark = now + 0.25
        pt = wintypes.POINT()
        if u32.GetCursorPos(ctypes.byref(pt)):
            cx, cy = self._sensor[2]
            if abs(pt.x - cx) > 10 or abs(pt.y - cy) > 10:
                self.reparks += 1
                u32.SetCursorPos(cx, cy)

    def _drop_sensor(self):
        if self._sensor:
            u32.DestroyWindow(self._sensor[0])
            if self._restore_pos:
                u32.SetCursorPos(*self._restore_pos)
            self._sensor = None

    def _run(self):
        try:
            self._loop()
        except Exception as e:  # reported by stop()
            self.error = f"{type(e).__name__}: {e}"
            self._ready.set()

    def _loop(self):
        flags = WINEVENT_OUTOFCONTEXT | (WINEVENT_SKIPOWNPROCESS if self.skip_own else 0)

        def hook(_h, event, hwnd, id_object, id_child, _tid, _time):
            try:
                if event == EVENT_SYSTEM_FOREGROUND:
                    self._on_foreground(hwnd)
                elif id_object == OBJID_WINDOW and id_child == 0 and hwnd:
                    self._on_window(hwnd, "hook")
            except Exception as e:
                self.error = f"hook: {type(e).__name__}: {e}"

        cb = HOOKPROC(hook)
        h1 = u32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, cb, 0, 0, flags)
        h2 = u32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, None, cb, 0, 0, flags)
        if not h1 or not h2:
            raise OSError("SetWinEventHook failed")
        app_handle = u32.LoadCursorW(None, IDC_APPSTARTING)
        if self.park_cursor:
            self._make_sensor()
        self.table.refresh(time.time())
        self._fg = u32.GetForegroundWindow()
        known = {h for h in self._enum_visible()}
        self._write({"ev": "start", "app_cursor": _hex(app_handle), "poll_ms": self.poll_s * 1000, "sensor": bool(self._sensor),
                     "root_pid": self.root_pid, "names": sorted(self.names), "cmd_contains": self.cmd_contains,
                     "foreground": (self._window_info(self._fg) or {}).get("title")}, related=True)
        self._ready.set()
        msg = wintypes.MSG()
        next_poll = 0.0
        try:
            while not self._stop.is_set():
                while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                now = time.time()
                if now >= next_poll:
                    next_poll = now + self.poll_s
                    for pid in self.table.refresh(now):
                        if pid != os.getpid() and self.related_by(pid):
                            rec = self.table.info[pid]
                            self._recent.append((now, rec["name"], pid))
                            self._write({"ev": "proc", "related_by": self.related_by(pid),
                                         **self.table.describe(pid), "ppid": rec["ppid"], "related": True})
                    self._recent = [r for r in self._recent if now - r[0] <= 5]
                    self._poll_cursor(app_handle)
                    self._repark(now)
                    visible = self._enum_visible()
                    for hwnd in visible:
                        if hwnd not in known:
                            known.add(hwnd)
                            self._on_window(hwnd, "poll")
                    known &= set(visible)
                time.sleep(0.002)
        finally:
            self._drop_sensor()
            u32.UnhookWinEvent(h1)
            u32.UnhookWinEvent(h2)

    def start(self):
        if os.name != "nt":
            raise OSError("window_probe needs Windows")
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, name="window-probe", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self.error:
            raise OSError(self.error)
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        if self.out:
            self.out.close()
        return self.summary()

    # ---- summary -----------------------------------------------------------------------------------------

    def summary(self):
        """{label: {windows, focus, appstarting, procs, unrelated_*}} for every mark, plus 'ALL'."""
        with self._lock:
            out = {label: dict(row) for label, row in self.counts.items()}
        out["ALL"] = {k: sum(r[k] for r in out.values()) for k in self.METRICS}
        return out

    def related_events(self, label=None):
        return [e for e in self.events if e.get("related") and e["ev"] in ("window", "focus", "cursor")
                and (label is None or e["label"] == label)]


def format_summary(summary):
    lines = [f"{'scenario':<34} {'windows':>7} {'focus':>6} {'busy-cursor':>11} {'spawns':>7}   (unrelated: win/focus/cursor)"]
    for label, r in summary.items():
        lines.append(f"{(label or '-')[:34]:<34} {r['windows']:>7} {r['focus']:>6} {r['appstarting']:>11} "
                     f"{r['procs']:>7}   ({r['unrelated_windows']}/{r['unrelated_focus']}/"
                     f"{r['unrelated_appstarting']})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="JSON lines file")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to watch (default 30)")
    ap.add_argument("--root-pid", type=int, help="only processes descending from this pid are related")
    ap.add_argument("--names", help="comma-separated process names that are related (default list with 'default')")
    ap.add_argument("--cmd-contains", action="append", help="a name match also needs this in its command line")
    ap.add_argument("--poll-ms", type=float, default=20.0)
    ap.add_argument("--park", action="store_true",
                    help="park the mouse on a transparent window of the probe so the busy cursor can show")
    ap.add_argument("--all", action="store_true", help="also write unrelated events")
    ap.add_argument("--run", nargs=argparse.REMAINDER, help="run this command, watch until it exits plus 2 s")
    args = ap.parse_args(argv)
    names = None
    if args.names:
        names = DEFAULT_NAMES if args.names == "default" else [n.strip() for n in args.names.split(",") if n.strip()]
    proc = None
    root = args.root_pid
    if args.run:
        proc = subprocess.Popen(args.run)
        root = proc.pid
    probe = Probe(root_pid=root, names=names, cmd_contains=args.cmd_contains, poll_ms=args.poll_ms, out=args.out,
                  keep_all=args.all, park_cursor=args.park).start()
    try:
        end = time.time() + args.duration
        while time.time() < end:
            if proc is not None and proc.poll() is not None:
                time.sleep(2)
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    summary = probe.stop()
    print(format_summary(summary))
    for e in probe.related_events():
        print(json.dumps(e, default=str)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
