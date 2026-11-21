"""Is the owner at the PC right now? Read from two real signals, never from the clock:

  idle time   how long since the last keyboard or mouse input in this session (GetLastInputInfo)
  locked      whether the session is locked: the input desktop is then the secure "Winlogon" desktop, which a
              normal process cannot open (OpenInputDesktop fails) or whose name is not "Default"

The owner is at the PC when the session is not locked and the last input is younger than the idle limit
(config.json "pc_presence_idle_s", default 300 s = 5 minutes). Off Windows, or when the API cannot be read, the
answer is "not at the PC" (Telegram then carries the message, as it always did).

    at_pc()        -> bool
    state()        -> {"at_pc", "idle_s", "locked", "limit_s", "why"}

The API is a seam: pass api=<object with idle_s() and locked()> in tests."""
import os

DEFAULT_IDLE_S = 300.0
SETTING = "pc_presence_idle_s"


def idle_limit_s():
    """The idle limit from config.json (read directly, as the voice line does), else 5 minutes."""
    try:
        from waggle.core import settings
        value = float(settings._config_value(SETTING, DEFAULT_IDLE_S))
    except (TypeError, ValueError, ImportError):
        return DEFAULT_IDLE_S
    return value if value > 0 else DEFAULT_IDLE_S


class _WinApi:
    """GetLastInputInfo and the input desktop, through ctypes (user32 and kernel32 only)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        self._ct, self._LII = ctypes, LASTINPUTINFO
        self._u32 = ctypes.WinDLL("user32", use_last_error=True)
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._u32.GetLastInputInfo.argtypes = (ctypes.POINTER(LASTINPUTINFO),)
        self._u32.GetLastInputInfo.restype = wintypes.BOOL
        self._k32.GetTickCount.restype = wintypes.DWORD
        self._u32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._u32.OpenInputDesktop.restype = wintypes.HANDLE
        self._u32.GetUserObjectInformationW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        self._u32.GetUserObjectInformationW.restype = wintypes.BOOL
        self._u32.CloseDesktop.argtypes = (wintypes.HANDLE,)

    def idle_s(self):
        info = self._LII()
        info.cbSize = self._ct.sizeof(info)
        if not self._u32.GetLastInputInfo(self._ct.byref(info)):
            raise OSError(self._ct.get_last_error(), "GetLastInputInfo failed")
        # Both counters are 32-bit milliseconds since boot; the difference survives the 49.7-day wrap.
        return ((self._k32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def locked(self):
        desk = self._u32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desk:
            return True  # the secure desktop (lock screen, sign-in) cannot be opened by a normal process
        try:
            from ctypes import wintypes
            buf = self._ct.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not self._u32.GetUserObjectInformationW(desk, 2, buf, self._ct.sizeof(buf), self._ct.byref(needed)):
                return False
            return buf.value.lower() != "default"
        finally:
            self._u32.CloseDesktop(desk)


_api = None


def _default_api():
    global _api
    if _api is None and os.name == "nt":
        _api = _WinApi()
    return _api


def state(idle_limit=None, api=None):
    """{"at_pc": bool, "idle_s": float | None, "locked": bool | None, "limit_s": float, "why": str}."""
    limit = float(idle_limit) if idle_limit is not None else idle_limit_s()
    out = {"at_pc": False, "idle_s": None, "locked": None, "limit_s": limit, "why": ""}
    try:
        a = api or _default_api()
        if a is None:
            out["why"] = "not Windows"
            return out
        out["locked"] = bool(a.locked())
        out["idle_s"] = round(float(a.idle_s()), 1)
    except Exception as e:  # an unreadable API means "not known to be here": Telegram as before
        out["why"] = f"cannot read presence: {type(e).__name__}: {e}"[:200]
        return out
    if out["locked"]:
        out["why"] = "the session is locked"
    elif out["idle_s"] >= limit:
        out["why"] = f"no input for {out['idle_s']:.0f} s (limit {limit:.0f} s)"
    else:
        out["at_pc"], out["why"] = True, f"input {out['idle_s']:.0f} s ago"
    return out


def at_pc(idle_limit=None, api=None):
    """True when the session is unlocked and the last keyboard or mouse input is younger than the idle limit."""
    return state(idle_limit, api)["at_pc"]


if __name__ == "__main__":
    import json
    print(json.dumps(state()))
