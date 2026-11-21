"""Five-field cron expressions (minute hour day-of-month month day-of-week), evaluated in wall-clock time.

Fields accept `*`, numbers, ranges `a-b`, steps `*/n` `a-b/n` `a/n`, lists `a,b`, month and weekday names
(jan..dec, sun..sat; 0 and 7 are both Sunday) and the macros @hourly @daily @midnight @weekly @monthly
@yearly @annually. When both day-of-month and day-of-week are restricted, a day matching either counts
(classic cron).

Daylight-saving rules, for a time zone object with PEP 495 `fold` support or local time (tz=None):
- a wall time that does not exist (spring forward) runs once, at the same offset past the jump
  (02:30 on the spring-forward day runs at 03:30);
- a wall time that happens twice (fall back) runs once, at its first occurrence.
"""
from datetime import datetime, timedelta
from functools import lru_cache

_MONTHS = {n: i + 1 for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DAYS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_FIELDS = (("minute", 0, 59, None), ("hour", 0, 23, None), ("day of month", 1, 31, None),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS))
_HORIZON_YEARS = 6


def _value(text, names, label):
    t = text.strip().lower()
    if names and t in names:
        return names[t]
    if not t.isdigit():
        raise ValueError(f"bad {label} value {text!r}")
    return int(t)


def _field(text, lo, hi, names, label):
    out = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"empty item in {label} field {text!r}")
        rng, _, step = part.partition("/")
        step = int(step) if step else 1
        if step < 1:
            raise ValueError(f"bad step in {label} field {text!r}")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (_value(x, names, label) for x in rng.split("-", 1))
        else:
            a = _value(rng, names, label)
            b = hi if "/" in part else a
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{label} field {text!r} is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr):
        self.expr = expr
        text = _MACROS.get(expr.strip().lower(), expr)
        parts = text.split()
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
        sets = [_field(p, lo, hi, names, label) for p, (label, lo, hi, names) in zip(parts, _FIELDS)]
        self.minutes = tuple(sorted(sets[0]))
        self.hours = tuple(sorted(sets[1]))
        self.doms = frozenset(sets[2])
        self.months = frozenset(sets[3])
        self.dows = frozenset(d % 7 for d in sets[4])
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def _day_ok(self, d):
        dom = d.day in self.doms
        dow = (d.weekday() + 1) % 7 in self.dows
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def next_after(self, ts, tz=None):
        """The first run strictly after epoch `ts`, as epoch seconds; None if none within six years."""
        c = to_wall(ts, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        last_year = c.year + _HORIZON_YEARS
        while c.year <= last_year:
            if c.month not in self.months:
                c = (c.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(c):
                c = (c + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if c.hour not in self.hours:
                h = next((x for x in self.hours if x > c.hour), None)
                c = (c + timedelta(days=1)).replace(hour=0, minute=0) if h is None else c.replace(hour=h, minute=0)
                continue
            m = next((x for x in self.minutes if x >= c.minute), None)
            if m is None:
                c = c.replace(minute=0) + timedelta(hours=1)
                continue
            c = c.replace(minute=m)
            t = from_wall(c, tz)
            if t > ts:
                return t
            c += timedelta(minutes=1)  # the first occurrence of a repeated wall time already passed
        return None


@lru_cache(maxsize=256)
def parse(expr):
    return Cron(expr)


def next_after(expr, ts, tz=None):
    return parse(expr).next_after(ts, tz)


def to_wall(ts, tz=None):
    """Epoch -> naive wall-clock datetime in `tz` (local time when None)."""
    if tz is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, tz).replace(tzinfo=None)


def from_wall(wall, tz=None):
    """Naive wall-clock datetime -> epoch, first occurrence for repeated times (fold=0)."""
    if tz is None:
        return wall.replace(fold=0).timestamp()
    return wall.replace(tzinfo=tz, fold=0).timestamp()
