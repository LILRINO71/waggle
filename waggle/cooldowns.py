"""An engine that just failed rests for a while, so the next call goes straight to the next engine (borrow-list row 13).

Today every call to Claude after a limit, a billing problem, an overload or a signed-out CLI spawns a process, waits
for it to fail again, and only then falls back to the local model. A cooldown per kind of error stops that:

    kind     what it is                                        rests
    limit    usage limit / rate limit                          5 min, or until the reset time in the error text
    credit   billing, credit balance, extra usage              60 min
    busy     overloaded, at capacity, 529                      2 min
    auth     signed out, token expired                         10 min
    missing  the CLI / the server is not installed or not up   30 min
    down     network, connection refused, 5xx, no answer       2 min

classify() maps an error (a ClaudeError's kind and text, an HTTP status, an exception) to one of these, or to None
when the error says nothing about the engine itself (a prompt that is too long, a bad answer): those never put an
engine to rest. note() starts a rest, cooling() asks whether an engine is resting, clear() ends it (a login does).
State is a small JSON file in the data home so the core, the hook and the CLI see the same rests; a damaged or
missing file means nothing is resting.

The idea (per-kind cooldown table, reset time read from the error text, first free engine in the order) comes from
Roamies' limits.mjs; this is a fresh implementation. Switch off with config.json "engine_cooldowns_enabled": false.
`python -m waggle.cooldowns status|clear [engine]`.
"""
import datetime as dt
import json
import os
import re
import sys
import time

from .config import Settings, data_path

MIN = 60.0
COOLDOWN = {"limit": 5 * MIN, "credit": 60 * MIN, "busy": 2 * MIN, "auth": 10 * MIN, "missing": 30 * MIN,
            "down": 2 * MIN}
MAX_REST_S = 6 * 3600.0   # even a reset time read from the text never rests an engine longer than this
FILE = "engine_cooldowns.json"

_CREDIT = ("credit balance", "billing", "payment required", "insufficient credit", "out of credits",
           "extra usage", "add credits", "purchase more", "exceeded your current quota", "insufficient_quota")
_BUSY = ("overloaded", "at capacity", "server is busy", "too busy", "529")
_AUTH = ("not logged in", "oauth", "unauthorized", "invalid api key", "invalid x-api-key", "authentication failed",
         "sign-in expired", "token expired")
_LIMIT = ("usage limit", "limit reached", "hit your limit", "rate limit", "rate_limit", "quota", "limit will reset",
          "too many requests", "429")
_DOWN = ("connection refused", "connection reset", "connection error", "econnrefused", "econnreset", "unreachable",
         "not reachable", "name or service not known", "getaddrinfo", "service unavailable", "bad gateway",
         "gateway timeout", "internal server error", "no route to host", "failed to establish")
_MISSING = ("cli not found", "not configured", "unknown engine")


def _has(low, words):
    return any(w in low for w in words)


def classify(kind="", text="", status=None, exc=None):
    """One of limit | credit | busy | auth | missing | down, or None when the error is not about the engine itself.
    kind: a ClaudeError kind (error, limit, auth, missing, model_mismatch, timeout, budget) or ''; status: an HTTP
    status code; exc: the exception, when there is one (an HTTPError carries its own status). A timeout of one slow
    call, a prompt that is too long, a bad answer and a model mismatch are not an engine outage: None."""
    low = f"{text or ''} {exc if exc is not None else ''}".lower()
    kind = (kind or "").lower()
    if status is None:
        code = getattr(exc, "code", None)
        m = re.search(r"\bhttp(?: error)?\s*(\d{3})\b", low)
        status = code if isinstance(code, int) else int(m.group(1)) if m else None
    if kind == "budget":
        return None  # Waggle's own allowance said wait: it re-checks itself
    if kind == "busy":
        return "busy"
    if kind == "missing" or isinstance(exc, FileNotFoundError) or _has(low, _MISSING):
        return "missing"
    if kind == "auth" or status in (401, 403) or _has(low, _AUTH):
        return "auth"
    if status == 402 or _has(low, _CREDIT):
        return "credit"
    if status == 529 or _has(low, _BUSY):
        return "busy"
    if kind == "limit" or status == 429 or _has(low, _LIMIT):
        return "limit"
    if status in (500, 502, 503, 504) or _has(low, _DOWN):
        return "down"
    if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):  # refused, reset, unreachable, URLError
        return "down"
    return None


_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_IN = re.compile(r"\bin\s+(?:about\s+|around\s+)?(?:(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b)?\s*"
                 r"(?:(\d+)\s*(?:minutes?|mins?|m)\b)?\s*(?:(\d+)\s*(?:seconds?|secs?|s)\b)?", re.I)


def reset_at(text, now=None):
    """When the error text says the limit resets, as epoch seconds; None when it does not say (or in the past).
    Reads "resets 3pm", "resets at 3:30 PM", "resets in 2h", "try again in 45 minutes", "in 1 hour 30 minutes"."""
    now = now or time.time()
    text = text or ""
    low = text.lower()
    if "reset" in low or "try again" in low or "retry" in low:
        for m in _IN.finditer(text):
            h, mi, sec = m.group(1), m.group(2), m.group(3)
            if h or mi or sec:
                s = float(h or 0) * 3600 + float(mi or 0) * 60 + float(sec or 0)
                if s > 0:
                    return now + s
        m = re.search(r"resets?\s*(?:at\s+)?" + _CLOCK.pattern, text, re.I)
        if m:
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            base = dt.datetime.fromtimestamp(now)
            t = base.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
            if t.timestamp() <= now:
                t += dt.timedelta(days=1)
            return t.timestamp()
    return None


def rest_seconds(kind, text="", now=None, reset_in_hours=None):
    """How long an engine rests after this kind of error: the per-kind default, or for a limit the time the error
    text (or reset_in_hours, which the Claude CLI wrapper has already read from it) says the limit resets."""
    now = now or time.time()
    base = COOLDOWN[kind]
    if kind == "limit":
        if isinstance(reset_in_hours, (int, float)) and not isinstance(reset_in_hours, bool) and reset_in_hours > 0:
            return min(max(reset_in_hours * 3600.0, 30.0), MAX_REST_S)
        at = reset_at(text, now)
        if at:
            return min(max(at - now, 30.0), MAX_REST_S)
    return base


# ---- state ---------------------------------------------------------------------------------------------------

def _load():
    try:
        d = json.loads(data_path(FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    p = data_path(FILE)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def enabled(settings=None):
    try:
        return bool(getattr(settings or Settings.load(), "engine_cooldowns_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def note(engine, kind, text="", now=None, settings=None, reset_in_hours=None):
    """Start (or extend) an engine's rest after an error of this kind. Returns the end time, or None when off/unknown.
    Never raises."""
    try:
        if kind not in COOLDOWN or not enabled(settings):
            return None
        now = now or time.time()
        until = now + rest_seconds(kind, text, now, reset_in_hours)
        d = _load()
        old = d.get(engine) or {}
        if old.get("until", 0) > until and old.get("kind") == kind:
            return old["until"]  # already resting at least this long
        d[engine] = {"kind": kind, "until": until, "at": now, "why": str(text or "")[:160]}
        _save(d)
        try:
            from . import events
            events.log("cooldown", engine=engine, error_kind=kind, until=until, minutes=round((until - now) / 60, 1))
        except Exception:  # noqa: BLE001
            pass
        return until
    except Exception:  # noqa: BLE001
        return None


def note_error(engine, kind="", text="", status=None, exc=None, now=None, settings=None, reset_in_hours=None):
    """classify() then note(). Returns the kind that was noted, or None."""
    k = classify(kind, text, status, exc)
    if k and note(engine, k, text or (str(exc) if exc is not None else ""), now, settings, reset_in_hours):
        return k
    return None


def cooling(engine, now=None, settings=None):
    """(resting, kind, until, why) for an engine. Not resting once the time is up, or when the switch is off."""
    if not enabled(settings):
        return False, "", 0.0, ""
    now = now or time.time()
    e = _load().get(engine)
    if not isinstance(e, dict):
        return False, "", 0.0, ""
    try:
        until = float(e.get("until", 0))
    except (TypeError, ValueError):
        return False, "", 0.0, ""
    if until <= now:
        return False, "", 0.0, ""
    return True, str(e.get("kind", "")), until, str(e.get("why", ""))


def describe(engine, now=None, settings=None):
    """'' when the engine is free, else a short phrase for error messages: 'resting after a limit error until 15:30'."""
    on, kind, until, _ = cooling(engine, now, settings)
    if not on:
        return ""
    return f"resting after a {kind} error until {dt.datetime.fromtimestamp(until):%H:%M}"


def clear(engine=None):
    """End one engine's rest (or all). Returns how many were cleared."""
    d = _load()
    if engine is None:
        n, d = len(d), {}
    else:
        n = 1 if d.pop(engine, None) is not None else 0
    if n:
        _save(d)
    return n


def first_free(order, now=None, settings=None):
    """The first engine of `order` that is not resting, else None."""
    return next((e for e in order if not cooling(e, now, settings)[0]), None)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["clear"]:
        print(f"cleared {clear(argv[1] if len(argv) > 1 else None)}")
        return 0
    now = time.time()
    rows = [(k, v) for k, v in _load().items() if isinstance(v, dict) and v.get("until", 0) > now]
    if not rows:
        print("no engine is resting")
    for k, v in rows:
        print(f"{k}: {v.get('kind')} until {dt.datetime.fromtimestamp(v['until']):%H:%M} ({v.get('why', '')[:80]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
