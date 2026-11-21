"""Pro-limit budget.

Anthropic does not publish Pro limits as numbers, so Waggle measures instead:
- It reads every Claude Code transcript on this PC (your own sessions too) and adds up
  weighted tokens in the current 5-hour window.
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


def window_usage(hours=5.0, now=None, roots=None):
    """Weighted units used in the last `hours` across all Claude Code transcripts + Waggle's ledger."""
    now = now or time.time()
    since = now - hours * 3600
    total, seen = 0.0, set()
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
                        if _ts(d.get("timestamp", "")) < since:
                            continue
                        mid = msg.get("id") or d.get("uuid")
                        if mid in seen:
                            continue
                        seen.add(mid)
                        total += weighted(msg["usage"], msg.get("model"))
            except OSError:
                continue
    # claude -p runs with --no-session-persistence leave no transcript; Waggle logs those itself.
    for row in ledger():
        if row.get("time", 0) >= since and row.get("headless"):
            total += row.get("units", 0)
    return total


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
    total = window_usage(s.window_hours, now) if used is None else used
    cap = learned_cap(s, total, now) if used is None else window_estimate(s)
    mine = waggle_usage(s.window_hours, now)
    # A rough cost of the call about to happen, so we stop before crossing the line, not after.
    expected = 0.6 * weight_for(model) if "opus" in model else 0.05
    # Before the first real limit hit the cap is only a lower bound, so "nearly full" can't be judged yet. At the
    # upper bound it is no lower bound any more: the window is bigger than the user's setting allows for.
    at_ceiling = cap >= estimate_bounds(s)[1]
    if (calibrated(st) or at_ceiling) and total + expected > s.stop_at * cap:
        if at_ceiling and total > cap:
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
    total = window_usage(s.window_hours)
    cap = learned_cap(s, total)
    return {"window_used_units": round(total, 3), "window_estimate_units": cap,
            "window_pct": round(100 * total / cap, 1),
            "waggle_units": round(waggle_usage(s.window_hours), 3),
            "blocked_until": _state().get("blocked_until")}
