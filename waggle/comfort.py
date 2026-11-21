"""Comfort settings the owner can change by asking in chat ("talk faster", "use a different voice", "quiet hours from
10 PM to 7"), through the MCP tool `settings` (waggle/waggle_mcp.py).

Only the keys in KEYS can be changed, each with its own validation. Nothing the guard hashes (guard.CONFIG_KEYS:
budgets, limits, write roots, the owner's addresses, the owner-instruction switch) is ever on the list, and a key that
is not on it is refused. A change is made only in the owner's own turn (guard.current_turn()["owner"]): the policy
(policy.owner_acts) lets the owner's turn through, any other turn asks, and this module refuses it again, so mail,
web pages, background jobs and agents can never change a setting.

A change keeps a backup of config.json (config.json.bak-settings-<time>, the newest five), is written through
Settings.save (only the one key), takes effect at once (every reader loads Settings fresh; the two daily notes are
core timers, which are updated here too) and is recorded as the event "settings_changed".
"""
import json
import math
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import config, events, guard
from .config import Settings

SPEED_RANGE = (0.7, 1.8)
KEEP_BACKUPS = 5
BACKUP_TAG = ".bak-settings-"
# The voices of Kokoro v1.0 that speak English (the speech code picks the accent from the first letter: b = British,
# otherwise American). Used only when the installed voice file cannot be read.
FALLBACK_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river",
    "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck",
    "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis")
ENGLISH_PREFIXES = ("af_", "am_", "bf_", "bm_")
MAIL_TIMER = "knowledge.mailwatch"
BRIEF_TIMER, EVENING_TIMER = "outreach.brief", "outreach.evening"

# setting -> (what it is, in plain words)
KEYS = {
    "voice_note_voice": "the voice Waggle speaks in (voice notes, the PC voice, calls)",
    "voice_note_speed": f"how fast Waggle speaks, {SPEED_RANGE[0]} to {SPEED_RANGE[1]} (1.0 is normal)",
    "quiet_hours": "the hours Waggle holds non-urgent messages: [start hour, end hour] 0-23, same hour = no quiet hours",
    "brief_time": "when the morning brief is sent (24-hour HH:MM)",
    "evening_time": "when the evening note is sent (24-hour HH:MM)",
    "mailwatch": "whether Waggle watches the inbox and texts about important mail: on or off",
}
ALIASES = {
    "voice": "voice_note_voice", "speaking_voice": "voice_note_voice", "speed": "voice_note_speed",
    "talk_speed": "voice_note_speed", "speaking_speed": "voice_note_speed", "quiet": "quiet_hours",
    "quiet_time": "quiet_hours", "brief": "brief_time", "morning_brief": "brief_time", "morning_brief_time": "brief_time",
    "evening": "evening_time", "evening_note": "evening_time", "evening_note_time": "evening_time",
    "mail_watch": "mailwatch", "mail": "mailwatch",
}
assert not set(KEYS) & set(guard.CONFIG_KEYS)  # a safety key never becomes changeable from chat


class SettingError(ValueError):
    """The request is refused; the message says why, in plain words for the owner."""


# ---- voices --------------------------------------------------------------------------------------------------

def _models_dir():
    return Path(os.environ.get("WAGGLE_KOKORO_MODELS", Path.home() / ".waggle" / "kokoro" / "models"))


def installed_voices():
    """The English Kokoro voices that are installed (read from the voice file the speech server loads), else the
    known set. Sorted."""
    names = []
    try:
        with zipfile.ZipFile(_models_dir() / "voices-v1.0.bin") as z:
            names = [n[:-4] for n in z.namelist() if n.endswith(".npy")]
    except (OSError, zipfile.BadZipFile):
        pass
    names = [n for n in names if n.startswith(ENGLISH_PREFIXES)] or list(FALLBACK_VOICES)
    return sorted(names)


def _voice(value):
    text = str(value or "").strip().lower().replace(" ", "_")
    voices = installed_voices()
    if text in voices:
        return text
    if text and "_" not in text:  # "george" -> bm_george when only one voice has that name
        hits = [v for v in voices if v.split("_", 1)[1] == text]
        if len(hits) == 1:
            return hits[0]
    raise SettingError(f"{value!r} is not an installed English voice. Installed: {', '.join(voices)}")


# ---- the other values ----------------------------------------------------------------------------------------

def _speed(value):
    if isinstance(value, bool):
        raise SettingError("speed must be a number")
    try:
        x = float(str(value).strip().lower().rstrip("x"))
    except (TypeError, ValueError):
        raise SettingError(f"{value!r} is not a number; speed is {SPEED_RANGE[0]} to {SPEED_RANGE[1]}") from None
    if not math.isfinite(x) or not SPEED_RANGE[0] <= x <= SPEED_RANGE[1]:
        raise SettingError(f"speed {value} is outside {SPEED_RANGE[0]} to {SPEED_RANGE[1]}")
    return round(x, 2)


def _hour(v):
    if isinstance(v, bool):
        raise SettingError("an hour must be a whole number from 0 to 23")
    try:
        if isinstance(v, float) and not v.is_integer():
            raise ValueError
        h = int(str(v).strip()) if not isinstance(v, (int, float)) else int(v)
    except (TypeError, ValueError):
        raise SettingError(f"{v!r} is not a whole hour from 0 to 23") from None
    if not 0 <= h <= 23:
        raise SettingError(f"hour {h} is outside 0 to 23")
    return h


def _quiet(value):
    if isinstance(value, str):
        value = [p for p in re.split(r"[\s,;/-]+|\bto\b", value.strip()) if p]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SettingError("quiet_hours takes exactly two hours: [start, end], e.g. [22, 7]")
    return [_hour(value[0]), _hour(value[1])]


_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$", re.I)


def _time(value):
    m = _TIME.match(str(value or "").strip())
    if not m:
        raise SettingError(f"{value!r} is not a time; use 24-hour HH:MM like 07:30 or 21:00")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if ap:
        if not 1 <= h <= 12:
            raise SettingError(f"{value!r} is not a valid am/pm time")
        h = h % 12 + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise SettingError(f"{value!r} is not a valid time")
    return f"{h:02d}:{mi:02d}"


def _onoff(value):
    if isinstance(value, bool):
        return value
    t = str(value).strip().lower()
    if t in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if t in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    raise SettingError(f"{value!r} is not on or off")


VALIDATE = {"voice_note_voice": _voice, "voice_note_speed": _speed, "quiet_hours": _quiet, "brief_time": _time,
            "evening_time": _time, "mailwatch": _onoff}


def _clock(spec, default):
    """HH:MM (or a 5-field cron, as brief_time may hold) -> HH:MM."""
    spec = str(spec or default).strip()
    parts = spec.split()
    try:
        if len(parts) == 5:
            return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        return _time(spec)
    except (ValueError, SettingError):
        return _clock(default, default) if spec != default else spec


def _hour_words(h):
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def _time_words(t):
    h, m = (int(x) for x in t.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def words(key, value):
    """One value in plain words."""
    if key == "quiet_hours":
        a, b = value
        return "off" if a == b else f"{_hour_words(a)} to {_hour_words(b)}"
    if key in ("brief_time", "evening_time"):
        return _time_words(value)
    if key == "mailwatch":
        return "on" if value else "off"
    if key == "voice_note_speed":
        return f"{value:g}x"
    return str(value)


# ---- reading -------------------------------------------------------------------------------------------------

def _mail_on():
    """True/False from the mail watch timer, None when the core has not created it yet."""
    try:
        from .core import timers
        t = timers.get(MAIL_TIMER)
        return None if t is None else bool(t["enabled"])
    except Exception:
        return None


def current(key, settings=None):
    s = settings or Settings.load()
    if key == "mailwatch":
        return _mail_on()
    if key == "quiet_hours":
        return [int(x) for x in s.quiet_hours]
    if key == "brief_time":
        return _clock(s.brief_time, "07:30")
    if key == "evening_time":
        return _clock(s.evening_time, "21:00")
    return getattr(s, key)


def get_all(settings=None):
    """{"settings": {key: {"value", "now", "what"}}, "voices": [...]} for the owner."""
    s = settings or Settings.load()
    out = {}
    for key, what in KEYS.items():
        v = current(key, s)
        out[key] = {"value": v, "now": "not set up yet" if v is None else words(key, v), "what": what}
    return {"settings": out, "voices": installed_voices()}


def resolve_key(key):
    k = str(key or "").strip().lower().replace(" ", "_").replace("-", "_")
    if k in guard.CONFIG_KEYS:
        raise SettingError(f"{k} is a safety setting; it can only be changed by editing config.json yourself, never from chat")
    k = ALIASES.get(k, k)
    if k not in KEYS:
        raise SettingError(f"{key!r} is not a setting that can be changed from chat. Changeable: {', '.join(KEYS)}")
    return k


# ---- writing -------------------------------------------------------------------------------------------------

def _backup():
    """Copy config.json aside and keep only the newest KEEP_BACKUPS of these copies."""
    path = Path(config.CONFIG_FILE)
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}{BACKUP_TAG}{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}{BACKUP_TAG}{time.strftime('%Y%m%d-%H%M%S')}-{n}")
    shutil.copy2(path, dest)
    old = sorted(path.parent.glob(path.name + BACKUP_TAG + "*"))
    for p in old[:-KEEP_BACKUPS]:
        try:
            p.unlink()
        except OSError:
            pass
    return dest


def _write(key, value):
    s = Settings.load()
    setattr(s, key, value)
    s.save()


def _retime(key):
    """The daily notes are core timers: move the timer to the setting now in config.json."""
    from .core import timers
    from . import tick
    tid = BRIEF_TIMER if key == "brief_time" else EVENING_TIMER
    for t, kind, spec, job_kind, policy, max_late in tick.timer_specs(Settings.load()):
        if t == tid:
            timers.add(t, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="outreach")
            return
    raise RuntimeError(f"timer {tid} is not defined")


def set_value(key, value, turn=None):
    """Change one setting. Returns {"ok", "key", "old", "new", "old_words", "new_words", "undo"}. Raises
    SettingError when the request is refused (not the owner's turn, key not allowed, value not valid)."""
    if not (isinstance(turn, dict) and turn.get("owner")):
        raise SettingError("settings change only when the owner asks in their own chat message; nothing was changed")
    key = resolve_key(key)
    new = VALIDATE[key](value)
    old = current(key)
    out = {"ok": True, "key": key, "old": old, "new": new,
           "old_words": "not set up yet" if old is None else words(key, old), "new_words": words(key, new)}
    out["undo"] = f'set {key} back to {json.dumps(old)}' if old is not None else ""
    if old == new:
        out["unchanged"] = True
        return out
    if key == "mailwatch":
        from .core import timers
        if old is None:
            raise SettingError("the mail watch timer does not exist yet (the core creates it at start); nothing was changed")
        if not (timers.enable(MAIL_TIMER) if new else timers.disable(MAIL_TIMER)):
            raise SettingError("the mail watch timer could not be changed; nothing was changed")
    else:
        backup = _backup()
        _write(key, new)
        if key in ("brief_time", "evening_time"):
            try:
                _retime(key)
            except Exception as e:  # keep config.json and the timer telling the same story
                _write(key, old)
                raise SettingError(f"the timer could not be moved ({type(e).__name__}: {e}); nothing was changed") from e
        out["backup"] = backup.name if backup else ""
    events.log("settings_changed", key=key, old=old, new=new, old_words=out["old_words"], new_words=out["new_words"],
               origin=str(turn.get("origin") or ""), chat=str(turn.get("chat") or ""), turn_id=str(turn.get("turn_id") or ""))
    return out


def run(args, turn=None):
    """The `settings` tool: action get | set. Returns JSON text."""
    action = str((args or {}).get("action") or "get").strip().lower()
    if action == "get":
        return json.dumps(get_all(), ensure_ascii=False)
    if action != "set":
        return json.dumps({"ok": False, "error": "action must be get or set"})
    try:
        return json.dumps(set_value((args or {}).get("key"), (args or {}).get("value"), turn), ensure_ascii=False)
    except SettingError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
