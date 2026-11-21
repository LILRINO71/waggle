"""The one off switch: a file named OFF in the Waggle home.

While the file exists and is valid, nothing of Waggle's may start or restart: the watchdog does not restart the
core, the core and the PC voice started at logon exit at once (logging why), `control.start()` refuses, and the
Telegram gateway answers every message with one line saying Waggle is off. `python -m waggle.core off [--for 2h]`
writes it and stops what runs; `python -m waggle.core on` removes it and starts everything again. With `--for`
the switch carries an `until` time and the watchdog switches Waggle back on by itself after it.

The file is one JSON object: {"off": true, "since": <epoch>, "until": <epoch or null>, "by": "...",
"all": <bool: the gateway, Kokoro and dashboard were stopped too>, "restart": ["pcvoice"], "reason": "..."}.
It is written to a temporary file and moved into place, so a reader never sees half of it.

FAIL-SAFE CHOICE: a switch file that exists but cannot be read or understood (empty, cut off, not JSON, wrong
fields, unreadable) counts as ON, Waggle keeps running. The owner's duties (reminders, mail watch) must not stop
silently because a file got damaged; a deliberate off always goes through `off`, which writes the file
atomically and reads it back, and reports failure if the switch did not take. `status` says out loud when it
ignores a damaged file. This module imports only the settings, so the Hermes plug-in (stdlib only) can use it."""
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from waggle.core import settings

FILE = "OFF"

log = logging.getLogger("waggle.core.offswitch")

_UNITS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)


@dataclass(frozen=True)
class State:
    present: bool = False  # a file exists
    valid: bool = True  # False: it exists but is unreadable or not a switch (counts as ON)
    since: float = 0.0
    until: float = 0.0  # 0 = until `on`
    by: str = ""
    all: bool = False
    restart: tuple = field(default_factory=tuple)  # what `off` stopped and `on` must start again
    reason: str = ""
    detail: str = ""  # why an invalid file was ignored

    def active(self, now=None):
        """Waggle is switched off right now."""
        now = time.time() if now is None else now
        return self.present and self.valid and (not self.until or now < self.until)

    def expired(self, now=None):
        """A valid `--for` switch whose time has run out: the watchdog switches Waggle back on."""
        now = time.time() if now is None else now
        return self.present and self.valid and bool(self.until) and now >= self.until


def parse_duration(text):
    """Seconds in '2h', '30m', '90s', '1d', '1h30m'. A bare number is refused: 2 what?"""
    raw = str(text or "").strip()
    found = _DURATION.findall(raw)
    if not found or _DURATION.sub("", raw).strip():
        raise ValueError(f"cannot read {text!r} as a time; use 2h, 30m, 1h30m, 90s or 1d")
    seconds = sum(float(n) * _UNITS[u.lower()] for n, u in found)
    if seconds < 1:
        raise ValueError("the time must be at least one second")
    return seconds


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    return float(value)


def read():
    """The switch as it is now. Missing file: ON (State()). Damaged file: ON, with `present` and `detail`."""
    p = settings.path(FILE)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return State()
    except OSError as e:
        return State(present=True, valid=False, detail=f"cannot read {p.name}: {type(e).__name__}")
    try:
        data = json.loads(raw.decode("utf-8-sig"))  # a hand-made file from PowerShell may carry a BOM
        if not isinstance(data, dict) or data.get("off") is not True:
            raise ValueError("not an off switch")
        until = data.get("until")
        until = 0.0 if until is None else _num(until)
        restart = data.get("restart") or []
        if not isinstance(restart, list):
            raise ValueError("restart is not a list")
        return State(present=True, since=_num(data.get("since", 0)), until=until, by=str(data.get("by") or ""),
                     all=data.get("all") is True, restart=tuple(str(x) for x in restart),
                     reason=str(data.get("reason") or ""))
    except (ValueError, UnicodeDecodeError) as e:
        return State(present=True, valid=False, detail=f"{p.name} is not a valid switch ({e})")


def is_off(now=None):
    return read().active(now)


def write(*, by="owner", until=0.0, all=False, restart=(), reason="", now=None):
    """Switch Waggle off: the file is written atomically and read back. Raises OSError when it did not take."""
    now = time.time() if now is None else float(now)
    p = settings.path(FILE)
    body = {"off": True, "since": now, "until": float(until) if until else None, "by": by, "all": bool(all),
            "restart": sorted(set(restart)), "reason": reason}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(tmp, p)
    back = read()
    if not (back.present and back.valid):
        raise OSError(f"the off switch was written but cannot be read back: {back.detail or 'unknown'}")
    return back


def clear():
    """Remove the switch (also a damaged one). Raises OSError when the file cannot be removed."""
    try:
        settings.path(FILE).unlink()
    except FileNotFoundError:
        pass


def _stamp(ts, now):
    t = time.localtime(ts)
    if time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", t)
    return time.strftime("%Y-%m-%d %H:%M", t)


def _left(seconds):
    if seconds < 90:
        return f"{max(1, int(seconds))} s"
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours >= 10 or abs(hours - round(hours)) < 0.05 else f"{hours:.1f} h"


def describe(state, now=None):
    """The status line(s) `status` prints."""
    now = time.time() if now is None else now
    if not state.present:
        return "Waggle switch: ON (no off switch set)"
    if not state.valid:
        return (f"Waggle switch: ON; the file {FILE} in the Waggle home is damaged and ignored ({state.detail}). "
                f"Run `python -m waggle.core off` to switch Waggle off properly, or `on` to remove the file.")
    who = f" by {state.by}" if state.by else ""
    scope = " (core, PC voice, gateway, Kokoro and dashboard stopped)" if state.all else " (core and PC voice stopped)"
    if state.active(now):
        end = (f"back on at {_stamp(state.until, now)} (in {_left(state.until - now)})" if state.until
               else "stays off until `python -m waggle.core on`")
        return f"Waggle switch: OFF since {_stamp(state.since, now)}{who}{scope}; {end}"
    return (f"Waggle switch: its off time ran out at {_stamp(state.until, now)}; the watchdog switches Waggle "
            f"back on within 10 s (or run `python -m waggle.core on`)")


def gateway_line(state, now=None):
    """The one line the Telegram gateway answers with while Waggle is off."""
    now = time.time() if now is None else now
    until = f" until {_stamp(state.until, now)}" if state.until else ""
    return f"Waggle is switched off{until}; send /on to switch it back on."


def refuse_if_off(what, now=None, logger=None):
    """For a process started at logon: returns True (and logs why, to `logger`) when it must exit at once."""
    out = logger or log
    state = read()
    if state.active(now):
        out.warning("%s not started: Waggle is switched off (%s)", what, describe(state, now))
        return True
    if state.present and not state.valid:
        out.warning("%s: ignoring a damaged off switch file (%s); starting", what, state.detail)
    return False
