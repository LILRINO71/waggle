"""Pro-limit budget.

Anthropic does not publish Pro limits as numbers, so Waggle measures instead:
- It reads every Claude Code transcript on this PC (your own sessions too) and adds up
  weighted tokens in the current 5-hour window. The real window is FIXED (it starts with the first message after the
  last reset and ends 5 h later), so the window start comes from the last real reading when there is one.
- Every `claude -p` stream-json run reports the real used share of the window (a rate_limit_event). note_real_usage
  stores the newest one in real_usage.json; a fresh reading is the used fraction, plus whatever was spent since, and
  it also calibrates the window size (cap = units counted / utilization).
- Each time a real "limit reached" error comes back, the total at that moment becomes a
  data point for the window size (moving average), so the estimate gets accurate over time.

Where the numbers live: config.json window_units_estimate is the user's own setting (the tamper guard hashes it);
Waggle never writes it. What Waggle learns goes to the `learned` table in budget.db (a path-protected file: an agent
needs the user's yes to write it) and is only ever used clamped to ESTIMATE_BOUNDS times the user's value, here in
this protected file. So nothing Waggle does by itself changes the approved settings, and nothing written into the
learned state can lift Waggle's share or stop line past 4x what the user set.

Weights: Sonnet = 1, Opus = 5/3, Haiku = 1/3 per input token; output counts 5x, cache reads
0.1x, cache writes 1.25x. Units are millions of weighted tokens.
"""
import datetime as dt
import json
import math
import os
import time
from pathlib import Path

from .config import Settings, data_path

TRANSCRIPTS = Path.home() / ".claude" / "projects"
# The learned window size stays within these multiples of the user's window_units_estimate (and above ESTIMATE_FLOOR).
ESTIMATE_BOUNDS = (0.1, 4.0)
ESTIMATE_FLOOR = 0.1
_DEFAULT_ESTIMATE = 6.0
MODEL_WEIGHT = {"opus": 5 / 3, "sonnet": 1.0, "haiku": 1 / 3, "fable": 5 / 3}


def weight_for(model):
    m = (model or "").lower()
    for key, w in MODEL_WEIGHT.items():
        if key in m:
            return w
    return 1.0


def weighted(usage, model):
    w = weight_for(model)
    units = (usage.get("input_tokens", 0)
             + 5 * usage.get("output_tokens", 0)
             + 0.1 * usage.get("cache_read_input_tokens", 0)
             + 1.25 * usage.get("cache_creation_input_tokens", 0))
    return w * units / 1e6


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _events(since, roots=None, until=None):
    """(timestamp, weighted units) for every usage seen in [since, until]: Claude Code transcripts + Waggle's ledger."""
    out, seen = [], set()
    for root in roots or [TRANSCRIPTS]:
        if not root.exists():
            continue
        for f in root.glob("*/*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"usage"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = d.get("message")
                        if d.get("type") != "assistant" or not isinstance(msg, dict) or not msg.get("usage"):
                            continue
                        ts = _ts(d.get("timestamp", ""))
                        if ts < since or (until is not None and ts > until):
                            continue
                        mid = msg.get("id") or d.get("uuid")
                        if mid in seen:
                            continue
                        seen.add(mid)
                        out.append((ts, weighted(msg["usage"], msg.get("model"))))
            except OSError:
                continue
    # claude -p runs with --no-session-persistence leave no transcript; Waggle logs those itself.
    for row in ledger():
        if row.get("time", 0) >= since and row.get("headless") and (until is None or row.get("time", 0) <= until):
            out.append((row.get("time", 0), row.get("units", 0)))
    return out


def _usage_from(since, roots=None, until=None):
    return sum(u for _, u in _events(since, roots, until))


def window_start(now=None, hours=5.0, roots=None):
    """When the current 5-hour window began. The real window is fixed: it starts with the first message after the
    last reset and resets `hours` later.
    - a real reading whose resets_at is still ahead: resets_at - hours
    - an expired reading: the first usage after its resets_at, but never earlier than now - hours
    - no reading: the rolling window (now - hours)."""
    now = now or time.time()
    rolling = now - hours * 3600
    r = _real_reading()
    if not r:
        return rolling
    if r["resets_at"] > now:
        return r["resets_at"] - hours * 3600
    floor = max(r["resets_at"], rolling)
    firsts = [ts for ts, _ in _events(floor, roots) if ts > r["resets_at"] and ts <= now]
    return min(firsts) if firsts else now


def window_usage(hours=5.0, now=None, roots=None):
    """Weighted units used in the current 5-hour window (fixed from the last real reading when there is one, else the
    last `hours` rolling) across all Claude Code transcripts + Waggle's ledger."""
    now = now or time.time()
    return _usage_from(window_start(now, hours, roots), roots)


def ledger():
    p = data_path("ledger.jsonl")
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def record(model, usage, purpose, headless=True, ok=True, error_kind=None):
    from . import budget
    c = budget.current()
    row = {"time": time.time(), "model": model, "purpose": purpose, "ok": ok, "headless": headless,
           "units": weighted(usage or {}, model), "error_kind": error_kind, "kind": c["kind"]}
    if c.get("objective"):
        row["objective"] = c["objective"]
    try:  # per-kind / per-objective accounting for the background budget
        budget.record(model, usage, purpose, ok=ok, error_kind=error_kind)
    except Exception:
        pass
    with open(data_path("ledger.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _state():
    p = data_path("quota_state.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---- the learned window size (budget.db, bounded) -----------------------------------------------------------

def _learned(key, con=None):
    """A learned value from budget.db's `learned` table (None when absent). con: an open budget.db connection, for
    callers inside one of its transactions (budget.check during reserve)."""
    from . import budget
    own = con is None
    con = con or budget._db()
    try:
        row = con.execute("SELECT value FROM learned WHERE key=?", (key,)).fetchone()
    finally:
        if own:
            con.close()
    return row[0] if row else None


def _set_learned(key, value, now=None):
    from . import budget
    con = budget._db()
    try:
        con.execute("INSERT OR REPLACE INTO learned VALUES (?,?,?)", (key, float(value), now or time.time()))
    finally:
        con.close()


def _number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def user_estimate(s):
    """The user's own window_units_estimate (config.json), or the default when it is not a positive number."""
    v = _number(getattr(s, "window_units_estimate", None))
    return v if v and v > 0 else _DEFAULT_ESTIMATE


def estimate_bounds(s):
    base = user_estimate(s)
    return max(ESTIMATE_FLOOR, base * ESTIMATE_BOUNDS[0]), max(ESTIMATE_FLOOR, base * ESTIMATE_BOUNDS[1])


def window_estimate(s, con=None):
    """The window size every budget check uses: the learned value clamped to estimate_bounds, or the user's own
    value while nothing was learned (or the learned state cannot be read)."""
    lo, hi = estimate_bounds(s)
    try:
        v = _number(_learned("window_estimate", con))
    except Exception:
        v = None
    return min(max(user_estimate(s) if v is None else v, lo), hi)


def calibrated(st=None):
    """A real limit hit has been seen, so the estimate is a measurement and not only a lower bound. Also read from
    budget.db, so deleting the samples in quota_state.json does not lift the stop line."""
    st = _state() if st is None else st
    if st.get("samples"):
        return True
    try:
        return bool(_learned("calibrated_at"))
    except Exception:
        return True  # unreadable: keep the stop line


def _learn_estimate(s, value, now=None):
    lo, hi = estimate_bounds(s)
    v = round(min(max(float(value), lo), hi), 3)
    _set_learned("window_estimate", v, now)
    return v


def _save_state(st):
    data_path("quota_state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")


# ---- the real reading from the CLI (rate_limit_event) ------------------------------------------------------------

FRESH_S = 1800          # a reading this young is the used fraction; older, only the estimate is used
CALIBRATE_MIN = 0.15    # below this much utilization the measured cap is too coarse to learn from
KEEP_CAPS = 5           # the cap is the mean of this many latest calibrations


def _epoch(v):
    """resetsAt as epoch seconds: a number (seconds or milliseconds) or an ISO string; None when unusable."""
    if isinstance(v, str):
        v = _ts(v) or None
    v = _number(v)
    if v is None or v <= 0:
        return None
    return v / 1000 if v > 1e11 else v


def _parse_info(info):
    """(utilization, resets_at) of the five-hour window out of a rate_limit_info dict, or None."""
    if not isinstance(info, dict):
        return None
    uw = info.get("unifiedWindows")
    win = uw.get("five_hour") if isinstance(uw, dict) else None
    if not isinstance(win, dict):
        if info.get("rateLimitType") != "five_hour":
            return None
        win = info
    u = _number(win.get("utilization"))
    r = _epoch(win.get("resetsAt", info.get("resetsAt")))
    if u is None or r is None or u < 0 or u > 1.5:
        return None
    return u, r


def _real_reading():
    """The stored reading {utilization, resets_at, seen_at, units_at_seen, caps}, or None."""
    try:
        d = json.loads(data_path("real_usage.json").read_text(encoding="utf-8"))
        u, r = _number(d.get("utilization")), _number(d.get("resets_at"))
        if u is None or r is None:
            return None
        return {"utilization": u, "resets_at": r, "seen_at": _number(d.get("seen_at")) or 0.0,
                "units_at_seen": _number(d.get("units_at_seen")) or 0.0,
                "caps": [c for c in (_number(x) for x in d.get("caps") or []) if c and c > 0]}
    except Exception:
        return None


def note_real_usage(info, now=None, settings=None, roots=None):
    """Store the real five-hour reading from a rate_limit_event (rate_limit_info). Never raises; bad input is ignored.
    Returns the stored reading, or None."""
    try:
        parsed = _parse_info(info)
        if not parsed:
            return None
        util, resets_at = parsed
        now = now or time.time()
        s = settings or Settings.load()
        units = _usage_from(resets_at - s.window_hours * 3600, roots, until=now)  # counted in this fixed window right now
        old = _real_reading()
        caps = (old or {}).get("caps", [])
        if util >= CALIBRATE_MIN and units > 0:
            caps = (caps + [units / util])[-KEEP_CAPS:]
            try:  # readings replace waiting for a real limit hit
                _learn_estimate(s, sum(caps) / len(caps), now)
                _set_learned("calibrated_at", now, now)
            except Exception:
                pass
        row = {"utilization": util, "resets_at": resets_at, "seen_at": now, "units_at_seen": round(units, 6),
               "caps": [round(c, 4) for c in caps]}
        p = data_path("real_usage.json")
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(row, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return row
    except Exception:
        return None


def _fresh_reading(now):
    r = _real_reading()
    if r and r["resets_at"] > now and 0 <= now - r["seen_at"] <= FRESH_S:
        return r
    return None


def window_view(s, now=None, con=None):
    """The one used-fraction every decision and the status use: {pct, used, cap, source, resets_at}.
    A fresh real reading: pct = utilization + (units now - units at the reading) / cap. Otherwise the estimate:
    units in the window / cap (raising the cap when usage proves it higher)."""
    now = now or time.time()
    total = window_usage(s.window_hours, now)
    r = _fresh_reading(now)
    if r:
        cap = window_estimate(s, con)
        pct = r["utilization"] + max(0.0, total - r["units_at_seen"]) / cap
        return {"pct": pct, "used": pct * cap, "cap": cap, "source": "real", "resets_at": r["resets_at"]}
    cap = learned_cap(s, total, now) if con is None else window_estimate(s, con)
    old = _real_reading()
    return {"pct": total / cap, "used": total, "cap": cap, "source": "estimate",
            "resets_at": old["resets_at"] if old and old["resets_at"] > now else None}


def window_used_units(s, now=None, con=None):
    """Units used in the window as every check judges it: pct * cap (the real number when a reading is fresh)."""
    return window_view(s, now, con)["used"]


def note_limit_hit(settings=None, reset_in_hours=None, now=None):
    """Called when the CLI says the limit is reached: calibrate and block until the reset."""
    s = settings or Settings.load()
    now = now or time.time()
    used = window_usage(s.window_hours, now)
    st = _state()
    samples = st.get("samples", [])
    if used > 0.2:
        samples = (samples + [round(used, 3)])[-10:]
        _learn_estimate(s, sum(samples) / len(samples), now)
        _set_learned("calibrated_at", now, now)
    st["samples"] = samples
    st["blocked_until"] = now + 3600 * (reset_in_hours or 1.0)
    _save_state(st)
    return st


def learned_cap(s, total, now=None):
    """Usage above the estimate without a limit hit proves the real cap is higher: raise it (within the bounds)."""
    now = now or time.time()
    cap = window_estimate(s)
    if total > cap and _state().get("blocked_until", 0) <= now:
        # Land well under stop_at: a 1.15x bump left usage at 87%, above the 85% stop line, so Waggle locked
        # itself out right after learning the cap was higher (found 2026-11-18 after the Max upgrade).
        cap = _learn_estimate(s, total / max(0.5, s.stop_at - 0.15), now)
    return cap


def waggle_usage(hours=5.0, now=None):
    now = now or time.time()
    return sum(r.get("units", 0) for r in ledger() if r.get("time", 0) >= now - hours * 3600)


def can_escalate(model, settings=None, now=None, used=None):
    """(allowed, reason). Waggle may spend its share but never push the total past stop_at."""
    s = settings or Settings.load()
    now = now or time.time()
    st = _state()
    if st.get("blocked_until", 0) > now:
        mins = int((st["blocked_until"] - now) / 60)
        return False, f"Claude limit reached; retry in about {mins} min"
    real = False
    if used is None:
        view = window_view(s, now)
        total, cap, real = view["used"], view["cap"], view["source"] == "real"
    else:
        total, cap = used, window_estimate(s)
    mine = waggle_usage(s.window_hours, now)
    # A rough cost of the call about to happen, so we stop before crossing the line, not after.
    expected = 0.6 * weight_for(model) if "opus" in model else 0.05
    # Before the first real limit hit the cap is only a lower bound, so "nearly full" can't be judged yet. At the
    # upper bound it is no lower bound any more: the window is bigger than the user's setting allows for.
    at_ceiling = cap >= estimate_bounds(s)[1]
    if (real or calibrated(st) or at_ceiling) and total + expected > s.stop_at * cap:
        if at_ceiling and total > cap and not real:
            return False, (f"Pro window use is past {ESTIMATE_BOUNDS[1]:g}x your window_units_estimate; raise that "
                           f"setting in config.json (then approve it: python -m waggle.guard approve)")
        return False, f"Pro window at {total / cap:.0%} of estimated limit; keeping the rest for you"
    if mine + expected > s.waggle_share * cap:
        return False, f"Waggle already used its {s.waggle_share:.0%} share this window"
    from . import budget
    if budget.is_background() and not budget.current().get("ticket"):  # background work also has its own daily allowance and a lower window line
        bok, bwhy, _ = budget.check(model, budget.current().get("purpose", ""), s, now, window_used=total)
        if not bok:
            budget.log_decision("defer", bwhy, budget.current().get("objective"), budget.current().get("step"), model)
            return False, bwhy
    return True, f"ok ({total / cap:.0%} of window used)"


def status(settings=None):
    s = settings or Settings.load()
    v = window_view(s)
    return {"window_used_units": round(v["used"], 3), "window_estimate_units": v["cap"],
            "window_pct": round(100 * v["pct"], 1), "source": v["source"], "resets_at": v["resets_at"],
            "waggle_units": round(waggle_usage(s.window_hours), 3),
            "blocked_until": _state().get("blocked_until")}
