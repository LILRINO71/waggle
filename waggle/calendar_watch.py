"""Calendar watch: the owner's Google Calendar inside Waggle, with no Claude usage and no OAuth.

Google Calendar gives every calendar a read-only "Secret address in iCal format" (Settings -> the calendar ->
Integrate calendar). That https address IS the credential: it lives ONLY in Windows Credential Manager (target
"waggle-calendar-ics:<name>", see waggle.wincred), never in config.json, the store, a log, a status line or an
error message. config.json `calendars` [{"name"}] lists the names (more than one calendar is fine).

What it does, with the standard library only (urllib, sqlite3, zoneinfo, a small ICS parser of its own):

  fetch    every 15 min, 06:00-22:45 local (timer calendar.fetch, detached). One GET per calendar with ETag /
           If-Modified-Since (the body is kept, so a 304 still rolls the 14-day window forward). Parses VEVENTs:
           SUMMARY, LOCATION, DTSTART/DTEND as UTC "Z", TZID or all-day VALUE=DATE, DURATION, RRULE (DAILY, WEEKLY
           with BYDAY/INTERVAL, MONTHLY by month day or "2nd Tuesday"/"last Friday", YEARLY; COUNT, UNTIL), EXDATE,
           RECURRENCE-ID overrides and STATUS:CANCELLED. Today 00:00 through the next 14 days is kept in
           ~/.waggle/calendar.db. A failed fetch keeps what was stored; three in a row send ONE text a day.
  heads-up every 5 min round the clock (timer calendar.headsup, in the core, sqlite only): a text before each timed
           event, default 30 min (config `calendar_headsup_min`, 0 = off), e.g.
               In 30 min: Robotics meeting (Room 204), 4:00-5:30 PM.
           All-day events get none. Sent through notify.deliver(..., origin="calendar", ref="<calendar>:<uid>:<start>"),
           so a heads-up goes out exactly once (the outbox key and the headsup table both guard it); a moved event
           is a new start and gets a new heads-up. Quiet hours (23:00-07:00): the heads-up is always an asked-for
           text (never held, no daily cap) and goes at once when the event itself falls in quiet hours (the owner
           planned to be up); an event after quiet hours whose heads-up moment is still inside them waits, and
           the 5-min tick sends it when quiet hours end with the true minutes left ("In 15 min").
  brief    today() / upcoming(days) / today_line(); waggle.brief.calendar_today uses today_line() once a calendar is
           set up (the Claude connector path stays only for owners who have not set one up).

Time zones. Output is in the PC's zone (config `calendar_timezone` may name an IANA zone instead). A TZID is read
by zoneinfo when the PC has tz data; on Windows without the tzdata package it is read from the calendar's own
VTIMEZONE block (Google writes one), then from a built-in table of the US zones (2007+ DST rules), and only then as
the PC's local time (status says so in "tz_notes"). The PC's own zone comes from the C runtime, so it is
DST-correct without any tz database.

    python -m waggle.calendar_watch login           name + secret address (hidden); tests it, then stores it
    python -m waggle.calendar_watch status          calendars, last good fetch, errors, time-zone notes
    python -m waggle.calendar_watch today           today's events from the store
    python -m waggle.calendar_watch run [--dry-run] fetch now and send due heads-ups (--dry-run writes and sends
                                                    nothing; prints what it found and what it WOULD send)
"""
import calendar as _calendar
import datetime as dt
import getpass
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

from . import wincred
from .config import Settings, data_path

PURPOSE = "calendar"
DB_NAME = "calendar.db"
CRED_PREFIX = "waggle-calendar-ics:"  # + the calendar name
FETCH_JOB, HEADSUP_JOB = "calendar.fetch", "calendar.headsup"
FETCH_TIMER, HEADSUP_TIMER = "calendar.fetch", "calendar.headsup"
FETCH_CRON = "*/15 6-22 * * *"  # 06:00-22:45 local
HEADSUP_CRON = "*/5 * * * *"
FETCH_MAX_LATE_S = 3600  # a missed fetch runs once at start if it is at most this late
FETCH_TIMEOUT_S = 30
MAX_BYTES = 12 * 1024 * 1024
WINDOW_DAYS = 14
DEFAULT_LEAD_MIN = 30
HEADSUP_GRACE_S = 300  # a heads-up is worth sending until a few minutes after the event began
WAKE_START, WAKE_END = (6, 0), (23, 0)
FAILS_BEFORE_NOTICE = 3
STALE_S = 30 * 3600  # the brief will not claim "nothing today" from a calendar not read for this long
MAX_ITER = 60000  # hard stop for a runaway RRULE
BRIEF_MAX_ITEMS, BRIEF_MAX_CHARS = 8, 400
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")


class CalError(Exception):
    """A calendar step failed. `kind` is a short reason class, never server text and never the address."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


# ---- time zones ---------------------------------------------------------------------------------------------

UTC = dt.timezone.utc


class LocalTz(dt.tzinfo):
    """The PC's own zone, through the C runtime (mktime / localtime), so DST is right without a tz database."""

    def utcoffset(self, d):
        if d is None:
            return None
        naive = d.replace(tzinfo=None)
        try:
            ts = time.mktime((naive.year, naive.month, naive.day, naive.hour, naive.minute, naive.second, 0, 0, -1))
            return dt.timedelta(seconds=_calendar.timegm(naive.timetuple()) - ts)
        except (OverflowError, OSError, ValueError):
            return dt.timedelta(seconds=-time.timezone)

    def dst(self, d):
        return dt.timedelta(0)

    def tzname(self, d):
        return time.tzname[0]

    def fromutc(self, d):
        ts = _calendar.timegm(d.replace(tzinfo=None).timetuple())
        try:
            off = time.localtime(ts).tm_gmtoff
        except (OverflowError, OSError, ValueError):
            off = -time.timezone
        return (d.replace(tzinfo=None) + dt.timedelta(seconds=off)).replace(tzinfo=self)


def _nth_weekday(year, month, n, wd):
    """The date of the n-th (1..5) or n-th from last (-1..-5) weekday `wd` (Mon=0) of a month, or None."""
    if n > 0:
        first = dt.date(year, month, 1)
        d = first + dt.timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    else:
        last = dt.date(year, month, _calendar.monthrange(year, month)[1])
        d = last - dt.timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == month and d.year == year else None


class UsTz(dt.tzinfo):
    """A US zone with the current (2007+) rules: DST from the 2nd Sunday of March 02:00 to the 1st Sunday of
    November 02:00 local. For the built-in fallback when the PC has no tz database."""

    def __init__(self, std_hours, name, dst=True):
        self.std = dt.timedelta(hours=std_hours)
        self.name, self.has_dst = name, dst

    def _edges(self, year):
        start = dt.datetime.combine(_nth_weekday(year, 3, 2, 6), dt.time(2))
        end = dt.datetime.combine(_nth_weekday(year, 11, 1, 6), dt.time(2))  # wall time while still on DST
        return start, end

    def utcoffset(self, d):
        if d is None:
            return None
        if not self.has_dst:
            return self.std
        start, end = self._edges(d.year)
        naive = d.replace(tzinfo=None)
        if naive >= end - dt.timedelta(hours=1) and naive < end:  # the repeated hour: first pass is DST
            return self.std + dt.timedelta(hours=1) if not getattr(d, "fold", 0) else self.std
        return self.std + dt.timedelta(hours=1) if start <= naive < end else self.std

    def dst(self, d):
        return self.utcoffset(d) - self.std

    def tzname(self, d):
        return self.name

    def fromutc(self, d):
        u = d.replace(tzinfo=None)
        if not self.has_dst:
            return (u + self.std).replace(tzinfo=self)
        start, end = self._edges(u.year)
        s_utc, e_utc = start - self.std, end - (self.std + dt.timedelta(hours=1))
        if s_utc <= u < e_utc:
            return (u + self.std + dt.timedelta(hours=1)).replace(tzinfo=self)
        out = (u + self.std).replace(tzinfo=self)
        return out.replace(fold=1) if e_utc <= u < e_utc + dt.timedelta(hours=1) else out


BUILTIN_ZONES = {
    "America/Los_Angeles": (-8, "PT", True), "America/Denver": (-7, "MT", True), "America/Chicago": (-6, "CT", True),
    "America/New_York": (-5, "ET", True), "America/Anchorage": (-9, "AKT", True), "America/Phoenix": (-7, "MST", False),
    "Pacific/Honolulu": (-10, "HST", False), "US/Pacific": (-8, "PT", True), "US/Mountain": (-7, "MT", True),
    "US/Central": (-6, "CT", True), "US/Eastern": (-5, "ET", True),
}
BUILTIN_UTC = {"UTC", "Etc/UTC", "GMT", "Etc/GMT", "Z"}


def builtin_zone(tzid):
    if tzid in BUILTIN_UTC:
        return UTC
    spec = BUILTIN_ZONES.get(tzid)
    return UsTz(*spec) if spec else None


class VTz(dt.tzinfo):
    """A zone read from a VTIMEZONE block: observances [(onset wall time, offset_from, offset_to)] by year."""

    def __init__(self, tzid, observances):
        self.tzid, self.obs = tzid, observances  # [(kind, dtstart naive, from_s, to_s, rule dict or None, [rdates])]
        self._cache = {}

    def _onsets(self, year):
        if year in self._cache:
            return self._cache[year]
        out = []
        for _, start, off_from, off_to, rule, rdates in self.obs:
            for d in rdates:
                out.append((d, off_from, off_to))
            if rule and rule.get("FREQ") == "YEARLY":
                if year >= start.year and (not rule.get("UNTIL") or str(year) <= rule["UNTIL"][:4]):
                    month = int(rule.get("BYMONTH") or start.month)
                    day = None
                    for part in (rule.get("BYDAY") or "").split(",")[:1]:
                        m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", part)
                        if m:
                            day = _nth_weekday(year, month, int(m.group(1) or 1), _WD[m.group(2)])
                    if day is None and rule.get("BYMONTHDAY"):
                        day = dt.date(year, month, int(rule["BYMONTHDAY"].split(",")[0]))
                    if day is None:
                        day = dt.date(year, month, min(start.day, _calendar.monthrange(year, month)[1]))
                    out.append((dt.datetime.combine(day, start.time()), off_from, off_to))
            elif not rule and start.year == year:
                out.append((start, off_from, off_to))
        out.sort(key=lambda x: x[0])
        self._cache[year] = out
        return out

    def utcoffset(self, d):
        if d is None:
            return None
        naive = d.replace(tzinfo=None)
        best = None
        for year in (naive.year - 1, naive.year):
            for onset, off_from, off_to in self._onsets(year):
                if onset <= naive:
                    best = off_to
        if best is None:
            first = min(self.obs, key=lambda o: o[1]) if self.obs else None
            best = first[2] if first else 0
        return dt.timedelta(seconds=best)

    def dst(self, d):
        return dt.timedelta(0)

    def tzname(self, d):
        return self.tzid


def local_tz():
    """The zone events are shown in: config `calendar_timezone` (IANA, when zoneinfo can read it), else the PC's."""
    name = str(_config("calendar_timezone", "") or "").strip()
    if name:
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(name)
        except Exception:  # noqa: BLE001
            z = builtin_zone(name)
            if z is not None:
                return z
    return LocalTz()


class Ctx:
    """What the parser needs to turn a TZID into a tzinfo, and what it had to fall back on (for status)."""

    def __init__(self, local, vtz=None):
        self.local, self.vtz, self.notes, self._cache = local, vtz or {}, set(), {}

    def tz(self, tzid):
        if not tzid:
            return self.local
        if tzid in self._cache:
            return self._cache[tzid]
        z = None
        keys = [tzid] + ([ "/".join(tzid.split("/")[-2:]) ] if tzid.count("/") > 1 else [])
        try:
            import zoneinfo
            for k in keys:
                try:
                    z = zoneinfo.ZoneInfo(k)
                    break
                except Exception:  # noqa: BLE001  (no tz data on this PC, or an unknown key)
                    continue
        except ImportError:
            pass
        if z is None and tzid in self.vtz:
            z = self.vtz[tzid]
            self.notes.add("no tz database on this PC: used the calendar's own VTIMEZONE for " + tzid)
        if z is None:
            for k in keys:
                z = builtin_zone(k)
                if z is not None:
                    self.notes.add("no tz database on this PC: used the built-in US rules for " + tzid)
                    break
        if z is None:
            z = self.local
            self.notes.add(f"unknown time zone {tzid!r}: treated as the PC's local time")
        self._cache[tzid] = z
        return z


# ---- ICS parsing ---------------------------------------------------------------------------------------------

_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_quoted(s, sep):
    parts, cur, quoted = [], [], False
    for ch in s:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_line(line):
    """(NAME, {PARAM: value}, value) or None."""
    quoted, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    if cut < 1:
        return None
    head, value = line[:cut], line[cut + 1:]
    bits = _split_quoted(head, ";")
    params = {}
    for p in bits[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return bits[0].strip().upper(), params, value


def unescape(text):
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_value_time(value, params, ctx):
    """A date (all-day) or an aware datetime from one DTSTART-like value."""
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(v) == 8 and v.isdigit()):
        return dt.datetime.strptime(v[:8], "%Y%m%d").date()
    utc = v.endswith("Z")
    v = v.rstrip("Zz")
    fmt = "%Y%m%dT%H%M%S" if len(v) >= 15 else "%Y%m%dT%H%M"
    naive = dt.datetime.strptime(v[:15] if len(v) >= 15 else v, fmt)
    return naive.replace(tzinfo=UTC) if utc else naive.replace(tzinfo=ctx.tz(params.get("TZID")))


def parse_duration(value):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_rrule(value):
    rule = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        if k:
            rule[k.strip().upper()] = v.strip().upper()
    return rule


def _offset_s(text):
    """"-0800" -> -28800."""
    t = text.strip()
    digits = t.lstrip("+-")
    return (-1 if t.startswith("-") else 1) * (int(digits[:2]) * 3600 + int(digits[2:4] or 0) * 60)


def _vtimezones(components):
    out = {}
    for comp in components:
        tzid, obs = None, []
        for name, params, value in comp["props"]:
            if name == "TZID":
                tzid = value.strip()
        for sub in comp["subs"]:
            vals = {}
            rdates = []
            for name, params, value in sub["props"]:
                if name == "RDATE":
                    for part in value.split(","):
                        try:
                            rdates.append(dt.datetime.strptime(part.strip()[:15], "%Y%m%dT%H%M%S"))
                        except ValueError:
                            pass
                else:
                    vals[name] = value
            try:
                start = dt.datetime.strptime(vals["DTSTART"].strip()[:15], "%Y%m%dT%H%M%S")
                rule = parse_rrule(vals["RRULE"]) if "RRULE" in vals else None
                obs.append((sub["kind"], start, _offset_s(vals["TZOFFSETFROM"]), _offset_s(vals["TZOFFSETTO"]),
                            rule, rdates))
            except (KeyError, ValueError):
                continue
        if tzid and obs:
            out[tzid] = VTz(tzid, obs)
    return out


def parse_ics(text, local=None):
    """-> (raw events, Ctx). Raw event: dict(uid, summary, location, status, start, end, duration, rrule, exdates,
    recurrence_id) with start/end a date (all-day) or an aware datetime. Events that cannot be read are skipped."""
    lines = [parse_line(x) for x in unfold(text)]
    comps, stack = [], []
    seen_cal = False
    for item in lines:
        if item is None:
            continue
        name, params, value = item
        if name == "BEGIN":
            kind = value.strip().upper()
            seen_cal = seen_cal or kind == "VCALENDAR"
            node = {"kind": kind, "props": [], "subs": []}
            if stack:
                stack[-1]["subs"].append(node)
            stack.append(node)
            if kind in ("VEVENT", "VTIMEZONE"):
                comps.append(node)
        elif name == "END":
            if stack:
                stack.pop()
        elif stack:
            stack[-1]["props"].append((name, params, value))
    if not seen_cal:
        raise CalError("not a calendar")
    ctx = Ctx(local or local_tz(), _vtimezones([c for c in comps if c["kind"] == "VTIMEZONE"]))
    events = []
    for comp in comps:
        if comp["kind"] != "VEVENT":
            continue
        try:
            ev = _raw_event(comp["props"], ctx)
        except (ValueError, KeyError, OverflowError):
            continue
        if ev:
            events.append(ev)
    return events, ctx


def _raw_event(props, ctx):
    ev = {"uid": "", "summary": "", "location": "", "status": "", "start": None, "end": None, "duration": None,
          "rrule": None, "exdates": [], "recurrence_id": None}
    for name, params, value in props:
        if name == "UID":
            ev["uid"] = value.strip()
        elif name == "SUMMARY":
            ev["summary"] = unescape(value)
        elif name == "LOCATION":
            ev["location"] = unescape(value)
        elif name == "STATUS":
            ev["status"] = value.strip().upper()
        elif name == "DTSTART":
            ev["start"] = parse_value_time(value, params, ctx)
        elif name == "DTEND":
            ev["end"] = parse_value_time(value, params, ctx)
        elif name == "DURATION":
            ev["duration"] = parse_duration(value)
        elif name == "RRULE":
            ev["rrule"] = parse_rrule(value)
        elif name == "EXDATE":
            for part in value.split(","):
                if part.strip():
                    ev["exdates"].append(parse_value_time(part, params, ctx))
        elif name == "RECURRENCE-ID":
            ev["recurrence_id"] = parse_value_time(value, params, ctx)
    if ev["start"] is None:
        return None
    if not ev["uid"]:
        ev["uid"] = f"noid-{ev['summary'][:30]}-{ev['start']}"
    return ev


# ---- recurrence ----------------------------------------------------------------------------------------------

def _parse_byday(text):
    out = []
    for part in (text or "").split(","):
        m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", part.strip())
        if m:
            out.append((int(m.group(1)) if m.group(1) else 0, _WD[m.group(2)]))
    return out


def _ints(text):
    return [int(x) for x in (text or "").split(",") if re.fullmatch(r"[+-]?\d+", x.strip())]


def _month_days(year, month, rule, byday, default_day):
    """The dates in one month a MONTHLY/YEARLY rule picks."""
    last = _calendar.monthrange(year, month)[1]
    days = set()
    for md in _ints(rule.get("BYMONTHDAY")):
        d = md if md > 0 else last + 1 + md
        if 1 <= d <= last:
            days.add(dt.date(year, month, d))
    if byday:
        for n, wd in byday:
            if n:
                d = _nth_weekday(year, month, n, wd)
                if d:
                    days.add(d)
            else:
                days.update(dt.date(year, month, x) for x in range(1, last + 1)
                            if dt.date(year, month, x).weekday() == wd)
    if not days and not byday and not rule.get("BYMONTHDAY") and default_day <= last:
        days.add(dt.date(year, month, default_day))
    return sorted(days)


def gen_dates(rule, d0, limit):
    """Dates (>= d0, <= limit, in order) a recurrence rule produces, COUNT applied. Returns (dates, unsupported)."""
    freq = rule.get("FREQ", "")
    interval = max(1, (_ints(rule.get("INTERVAL")) or [1])[0])
    count = (_ints(rule.get("COUNT")) or [None])[0]
    byday = _parse_byday(rule.get("BYDAY"))
    bymon = _ints(rule.get("BYMONTH"))
    wkst = _WD.get(rule.get("WKST", "MO"), 0)
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [d0], True
    out, n = [], 0
    for k in range(MAX_ITER):
        if freq == "DAILY":
            base = d0 + dt.timedelta(days=k * interval)
            cands = [base]
            if byday:
                cands = [x for x in cands if x.weekday() in {wd for _, wd in byday}]
            if bymon:
                cands = [x for x in cands if x.month in bymon]
            if rule.get("BYMONTHDAY"):
                cands = [x for x in cands if x.day in _ints(rule["BYMONTHDAY"])]
            first = base
        elif freq == "WEEKLY":
            ws = d0 - dt.timedelta(days=(d0.weekday() - wkst) % 7) + dt.timedelta(weeks=k * interval)
            wds = sorted({wd for _, wd in byday} or {d0.weekday()}, key=lambda w: (w - wkst) % 7)
            cands = [ws + dt.timedelta(days=(w - wkst) % 7) for w in wds]
            if bymon:
                cands = [x for x in cands if x.month in bymon]
            first = ws
        elif freq == "MONTHLY":
            idx = d0.year * 12 + d0.month - 1 + k * interval
            y, m = idx // 12, idx % 12 + 1
            cands = _month_days(y, m, rule, byday, d0.day)
            if bymon:
                cands = [x for x in cands if x.month in bymon]
            first = dt.date(y, m, 1)
        else:  # YEARLY
            y = d0.year + k * interval
            cands = []
            for m in bymon or [d0.month]:
                cands += _month_days(y, m, rule, byday, d0.day)
            first = dt.date(y, 1, 1)
        if first > limit:
            break
        for d in cands:
            if d < d0:
                continue
            if d > limit:
                return out, False
            out.append(d)
            n += 1
            if count is not None and n >= count:
                return out, False
    return out, False


def _until(ev, rule):
    """('date', date) or ('ts', epoch) for UNTIL, or None."""
    raw = rule.get("UNTIL")
    if not raw:
        return None
    start = ev["start"]
    try:
        if len(raw) == 8:
            d = dt.datetime.strptime(raw, "%Y%m%d").date()
            if isinstance(start, dt.datetime):
                return ("ts", dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), tzinfo=start.tzinfo).timestamp() - 1)
            return ("date", d)
        naive = dt.datetime.strptime(raw.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
        if raw.endswith("Z"):
            aware = naive.replace(tzinfo=UTC)
            return ("ts", aware.timestamp()) if isinstance(start, dt.datetime) else ("date", naive.date())
        return ("ts", naive.replace(tzinfo=start.tzinfo).timestamp()) if isinstance(start, dt.datetime) \
            else ("date", naive.date())
    except ValueError:
        return None


def _local_midnight(d, tz):
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _ids(start_ts, all_day, d):
    """The identities an EXDATE / RECURRENCE-ID value can match one occurrence by."""
    return {f"d:{d.isoformat()}"} if all_day else {f"t:{int(start_ts)}", f"d:{d.isoformat()}"}


def _moment_id(m):
    return f"d:{m.isoformat()}" if not isinstance(m, dt.datetime) else f"t:{int(m.timestamp())}"


def _instances(ev, win_start, win_end, ctx):
    """Every occurrence of one master event that overlaps [win_start, win_end): dicts with start, end, all_day,
    start_day, end_day, ids. Sets ctx.notes when a rule part is not supported."""
    start, end, tz = ev["start"], ev["end"], ctx.local
    all_day = not isinstance(start, dt.datetime)
    if all_day:
        days = max(1, (end - start).days) if isinstance(end, dt.date) and not isinstance(end, dt.datetime) else (
            max(1, ev["duration"].days) if ev["duration"] else 1)
    else:
        if isinstance(end, dt.datetime):
            dur = max(0.0, end.timestamp() - start.timestamp())
        elif ev["duration"] is not None:
            dur = max(0.0, ev["duration"].total_seconds())
        else:
            dur = 0.0
    if ev["rrule"]:
        limit = dt.datetime.fromtimestamp(win_end, UTC).date() + dt.timedelta(days=2)
        d0 = start if all_day else start.replace(tzinfo=None).date()
        dates, unsupported = gen_dates(ev["rrule"], d0, limit)
        if unsupported:
            ctx.notes.add(f"repeat rule {ev['rrule'].get('FREQ', '?')} is not supported: only its first event is shown")
        until = _until(ev, ev["rrule"])
    else:
        dates, until = [start if all_day else start.replace(tzinfo=None).date()], None
    out = []
    for d in dates:
        if all_day:
            if until and until[0] == "date" and d > until[1]:
                break
            s_ts, e_ts = _local_midnight(d, tz), _local_midnight(d + dt.timedelta(days=days), tz)
            sd, ed = d, d + dt.timedelta(days=days)
        else:
            aware = dt.datetime.combine(d, start.replace(tzinfo=None).time()).replace(tzinfo=start.tzinfo)
            s_ts = aware.timestamp()
            if until and until[0] == "ts" and s_ts > until[1]:
                break
            e_ts = s_ts + dur
            sd = dt.datetime.fromtimestamp(s_ts, tz).date()
            ed = dt.datetime.fromtimestamp(max(e_ts, s_ts), tz).date()
        out.append({"start": s_ts, "end": e_ts, "all_day": all_day, "start_day": sd.isoformat(),
                    "end_day": ed.isoformat(), "ids": _ids(s_ts, all_day, d)})
    return [o for o in out if (o["end"] > win_start or o["start"] >= win_start) and o["start"] < win_end]


def occurrences(events, win_start, win_end, ctx):
    """Every visible occurrence in [win_start, win_end): masters expanded, EXDATEs and cancellations removed,
    RECURRENCE-ID overrides applied. Sorted by start. Each: uid, key, start, end, all_day, start_day, end_day,
    summary, location."""
    overrides = defaultdict(list)
    plain = []
    for ev in events:
        (overrides[ev["uid"]] if ev["recurrence_id"] is not None else plain).append(ev)
    out = []
    by_uid = {e["uid"]: e for e in plain}

    def add(ev, inst, master=None):
        m = master or ev
        key = f"{ev['uid']}@{int(inst['start'])}" if not inst["all_day"] else f"{ev['uid']}@{inst['start_day']}"
        out.append({"uid": ev["uid"], "key": key, "start": inst["start"], "end": inst["end"],
                    "all_day": inst["all_day"], "start_day": inst["start_day"], "end_day": inst["end_day"],
                    "summary": " ".join((ev["summary"] or m["summary"] or "").split()) or "(no title)",
                    "location": " ".join((ev["location"] or m["location"] or "").split())})

    for ev in plain:
        if ev["status"] == "CANCELLED":
            continue
        ovr = overrides.get(ev["uid"], [])
        replaced = {_moment_id(o["recurrence_id"]) for o in ovr}
        excluded = {_moment_id(x) for x in ev["exdates"]}
        for inst in _instances(ev, win_start, win_end, ctx):
            if inst["ids"] & (replaced | excluded):
                continue
            add(ev, inst)
    for uid, ovr in overrides.items():
        master = by_uid.get(uid)
        for o in ovr:
            if o["status"] == "CANCELLED":
                continue
            o = dict(o, rrule=None)
            for inst in _instances(o, win_start, win_end, ctx):
                add(o, inst, master)
    out.sort(key=lambda x: (x["start"], x["summary"]))
    return out


# ---- fetching ------------------------------------------------------------------------------------------------

def normalize_url(raw):
    """The secret address as one https URL (webcal:// is https), or raises CalError('not a web address')."""
    url = "".join(str(raw or "").split())
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if not re.match(r"https://[^/\s]+/\S+", url, re.I):
        raise CalError("not an https address")
    return url


def fetch_url(url, etag="", last_modified="", timeout=FETCH_TIMEOUT_S):
    """(status, text or None, etag, last_modified): 200 with the body, or 304. Raises CalError with a short kind;
    the address is never in a message (the exceptions are chained away: they can carry it)."""
    headers = {"User-Agent": "Waggle-calendar/1", "Accept": "text/calendar, text/plain, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        req = urllib.request.Request(normalize_url(url), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES + 1)
            got_etag, got_lm = r.headers.get("ETag", "") or "", r.headers.get("Last-Modified", "") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, etag, last_modified
        raise CalError(f"http {e.code}") from None
    except CalError:
        raise
    except (socket.timeout, TimeoutError):
        raise CalError("timeout") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise CalError("network") from None
    if len(raw) > MAX_BYTES:
        raise CalError("too big")
    return 200, raw.decode("utf-8", errors="replace"), got_etag, got_lm


FETCH = fetch_url  # tests replace it; nothing else should


# ---- settings and credentials -------------------------------------------------------------------------------

def _config(key, default):
    from waggle.core import settings as core_settings
    return core_settings._config_value(key, default)


def _set_config(key, value):
    """Set one key in config.json, keeping every other key (atomic write; the file is backed up first)."""
    import shutil
    from . import config
    path = config.CONFIG_FILE
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-cal-{time.strftime('%Y%m%d-%H%M%S')}"))
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def address(name):
    """The calendar's secret address, only ever from Credential Manager (None when none is stored)."""
    found = wincred.read(CRED_PREFIX + name)
    return found[1] if found and found[1] else None


def calendars():
    """Names of the calendars that are set up: config.json `calendars` [{"name"}], each with a stored address."""
    names = []
    for c in _config("calendars", []) or []:
        n = str((c.get("name") if isinstance(c, dict) else c) or "").strip().lower()
        if _NAME.fullmatch(n) and n not in names:
            names.append(n)
    if not names and address("personal"):
        names.append("personal")
    return [n for n in names if address(n)]


def configured():
    return bool(calendars())


def lead_minutes():
    try:
        return max(0, min(240, int(_config("calendar_headsup_min", DEFAULT_LEAD_MIN))))
    except (TypeError, ValueError):
        return DEFAULT_LEAD_MIN


# ---- the store ----------------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path(DB_NAME), timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS events (cal TEXT, key TEXT, uid TEXT, start_ts REAL, end_ts REAL, all_day INTEGER,
        start_day TEXT, end_day TEXT, summary TEXT, location TEXT, PRIMARY KEY (cal, key));
    CREATE INDEX IF NOT EXISTS events_start ON events(start_ts);
    CREATE TABLE IF NOT EXISTS feeds (cal TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, body TEXT, fetched_at REAL,
        ok_at REAL, fails INTEGER DEFAULT 0, error TEXT DEFAULT '', n_events INTEGER DEFAULT 0, notes TEXT DEFAULT '',
        notice_day TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS headsup (key TEXT PRIMARY KEY, cal TEXT, uid TEXT, start_ts REAL, sent_at REAL,
        outbox_id INTEGER);
    """)
    return con


def _read_only():
    p = data_path(DB_NAME)
    if not p.exists():
        return None
    return _db()


def _window(now, tz):
    d = dt.datetime.fromtimestamp(now, tz).date()
    return _local_midnight(d, tz), now + WINDOW_DAYS * 86400


def _expand(body, now):
    tz = local_tz()
    events, ctx = parse_ics(body, tz)
    start, end = _window(now, tz)
    return occurrences(events, start, end, ctx), ctx


def _store_events(con, cal, occs):
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM events WHERE cal=?", (cal,))
        con.executemany("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(cal, o["key"], o["uid"], o["start"], o["end"], int(o["all_day"]), o["start_day"],
                          o["end_day"], o["summary"], o["location"]) for o in occs])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _feed(con, cal):
    return con.execute("SELECT * FROM feeds WHERE cal=?", (cal,)).fetchone()


def _save_feed(con, cal, **fields):
    con.execute("INSERT OR IGNORE INTO feeds (cal) VALUES (?)", (cal,))
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE feeds SET {sets} WHERE cal=?", (*fields.values(), cal))


def _rows(con, start, end, cal=None):
    sql = "SELECT * FROM events WHERE end_ts>? AND start_ts<?"
    args = [start, end]
    if cal:
        sql += " AND cal=?"
        args.append(cal)
    return [dict(r) for r in con.execute(sql + " ORDER BY start_ts, summary", args).fetchall()]


# ---- the text -----------------------------------------------------------------------------------------------

def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def short_place(loc, limit=32):
    """The first comma-separated parts of a location that fit `limit` characters ("Room 204, Lincoln High School")."""
    lines = str(loc or "").strip().splitlines()
    parts = [p.strip() for p in (lines[0] if lines else "").split(",") if p.strip()]
    if not parts:
        return ""
    out = parts[0][:60]
    for p in parts[1:]:
        if len(out) + 2 + len(p) > limit:
            break
        out += ", " + p
    return out


def _clock(ts, tz):
    d = dt.datetime.fromtimestamp(ts, tz)
    return f"{d.hour % 12 or 12}:{d.minute:02d}", "AM" if d.hour < 12 else "PM"


def time_range(o, tz):
    """"4:00-5:30 PM", "11:30 AM-1:00 PM", or "4:00 PM" when there is no end (or it is on another day)."""
    s, sap = _clock(o["start"], tz)
    if o["end"] > o["start"] and dt.datetime.fromtimestamp(o["end"], tz).date() == \
            dt.datetime.fromtimestamp(o["start"], tz).date():
        e, eap = _clock(o["end"], tz)
        return f"{s}-{e} {sap}" if sap == eap else f"{s} {sap}-{e} {eap}"
    return f"{s} {sap}"


def _title(o):
    place = short_place(o.get("location"))
    return _clip(o["summary"], 80) + (f" ({place})" if place else "")


def headsup_text(o, now, tz):
    left = max(1, round((o["start"] - now) / 60))
    lead = f"{left} min" if left < 60 else (f"{left // 60} hr" + (f" {left % 60} min" if left % 60 else ""))
    return f"In {lead}: {_title(o)}, {time_range(o, tz)}."


def event_line(o, tz):
    if o["all_day"]:
        return f"all day: {_title(o)}"
    return f"{time_range(o, tz)} {_title(o)}"


# ---- reading what is stored ---------------------------------------------------------------------------------

def today(now=None, settings=None):
    """Today's events (local day) from the store, all-day ones first: dicts like occurrences()."""
    now = time.time() if now is None else now
    tz = local_tz()
    day0, _ = _window(now, tz)
    day1 = _local_midnight(dt.datetime.fromtimestamp(now, tz).date() + dt.timedelta(days=1), tz)
    con = _read_only()
    if con is None:
        return []
    try:
        rows = _rows(con, day0, day1)
    finally:
        con.close()
    rows = [r for r in rows if r["all_day"] or day0 <= r["start_ts"] < day1]
    return sorted(({**r, "start": r["start_ts"], "end": r["end_ts"], "all_day": bool(r["all_day"])} for r in rows),
                  key=lambda r: (not r["all_day"], r["start"], r["summary"]))


def upcoming(days=WINDOW_DAYS, now=None, settings=None):
    """Events that have not ended yet and start within `days` days, soonest first."""
    now = time.time() if now is None else now
    con = _read_only()
    if con is None:
        return []
    try:
        rows = _rows(con, now, now + days * 86400)
    finally:
        con.close()
    return [{**r, "start": r["start_ts"], "end": r["end_ts"], "all_day": bool(r["all_day"])} for r in rows]


def last_ok(con=None):
    own = con is None
    con = con or _read_only()
    if con is None:
        return None
    try:
        row = con.execute("SELECT MAX(ok_at) FROM feeds").fetchone()
        return row[0]
    finally:
        if own:
            con.close()


def today_line(settings=None, now=None):
    """One line for the morning brief ("4:00-5:30 PM Robotics meeting (Room 204); 6:00 PM Study group"), or "no events",
    or a plain note when the calendar has not been read lately. Never raises and never calls Claude."""
    try:
        now = time.time() if now is None else now
        ok = last_ok()
        tz = local_tz()
        if not ok or now - ok > STALE_S:
            when = f" since {dt.datetime.fromtimestamp(ok, tz):%a} {' '.join(_clock(ok, tz))}" if ok else ""
            return f"not read{when}, so today's events may be missing"
        items = today(now)
        if not items:
            return "no events"
        shown = [event_line(o, tz) for o in items[:BRIEF_MAX_ITEMS]]
        text = "; ".join(shown) + (f"; +{len(items) - BRIEF_MAX_ITEMS} more" if len(items) > BRIEF_MAX_ITEMS else "")
        return _clip(text, BRIEF_MAX_CHARS)
    except Exception:  # noqa: BLE001
        return ""


# ---- fetch duty ----------------------------------------------------------------------------------------------

def in_waking_hours(now):
    t = dt.datetime.fromtimestamp(now)
    m = t.hour * 60 + t.minute
    return WAKE_START[0] * 60 + WAKE_START[1] <= m <= WAKE_END[0] * 60 + WAKE_END[1]


def reason(kind):
    if kind in ("http 403", "http 404", "http 410"):
        return ("Google no longer accepts the secret address (it may have been reset in Calendar settings). "
                "Run: python -m waggle.calendar_watch login")
    if kind == "not a calendar":
        return "the address did not return a calendar"
    if kind == "timeout":
        return "it did not answer in time"
    if kind == "network":
        return "no connection to Google"
    return kind


def _fetch_one(con, cal, url, now, dry_run, fetch):
    """One calendar. Returns its report dict. Raises CalError when the fetch or parse fails."""
    row = None if dry_run else _feed(con, cal)
    etag = (row["etag"] if row else "") or ""
    lm = (row["last_modified"] if row else "") or ""
    status, body, new_etag, new_lm = fetch(url, etag, lm)
    if status == 304:
        body = row["body"] if row else None
        if not body:
            status, body, new_etag, new_lm = fetch(url, "", "")
    if not body:
        raise CalError("not a calendar")
    try:
        occs, ctx = _expand(body, now)
    except CalError:
        raise
    except Exception:  # noqa: BLE001  (a parser bug on odd input must not take the duty down)
        raise CalError("could not read the calendar") from None
    rep = {"calendar": cal, "events": len(occs), "not_modified": status == 304, "tz_notes": sorted(ctx.notes)}
    if dry_run:
        rep["occurrences"] = occs
        return rep
    _store_events(con, cal, occs)
    _save_feed(con, cal, etag=new_etag or "", last_modified=new_lm or "", body=body, fetched_at=now, ok_at=now,
               fails=0, error="", n_events=len(occs), notes="; ".join(sorted(ctx.notes)))
    return rep


def _tell_failure(con, s, now, cal, kind):
    """ONE text per calendar per day after FAILS_BEFORE_NOTICE failed fetches in a row."""
    from . import mailwatch, notify
    day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    row = _feed(con, cal)
    if not row or row["fails"] < FAILS_BEFORE_NOTICE or row["notice_day"] == day:
        return False
    notify.deliver("text", f"I can't read your calendar [{cal}]: {reason(kind)}", origin=PURPOSE,
                   ref=f"fail:{cal}:{day}", topic=f"calendar:fail:{cal}", chat=mailwatch.owner_chat(s), settings=s,
                   now=now, expires=now + 12 * 3600)
    _save_feed(con, cal, notice_day=day)
    return True


def run_fetch(settings=None, now=None, dry_run=False, fetch=None, force=False):
    """One fetch of every set-up calendar. Never raises for a calendar that fails (it is counted; three in a row
    send one text a day). dry_run reads the real calendar and writes nothing."""
    s = settings or Settings.load()
    now = time.time() if now is None else now
    if not dry_run and not force and not in_waking_hours(now):
        return {"skipped": "outside waking hours (06:00-23:00)"}
    names = calendars()
    if not names:
        return {"skipped": "no calendar set up (run: python -m waggle.calendar_watch login)"}
    fetch = fetch or FETCH
    con = None if dry_run else _db()  # a dry run opens no store at all
    out = {"calendars": [], "errors": {}}
    try:
        for cal in names:
            url = address(cal)
            try:
                out["calendars"].append(_fetch_one(con, cal, url, now, dry_run, fetch))
            except CalError as e:
                out["errors"][cal] = e.kind
                if not dry_run:
                    prev = _feed(con, cal)
                    _save_feed(con, cal, fetched_at=now, error=e.kind, fails=(prev["fails"] if prev else 0) + 1)
                    if _tell_failure(con, s, now, cal, e.kind):
                        out.setdefault("told", []).append(cal)
            except Exception as e:  # noqa: BLE001  (never the address: only the class)
                out["errors"][cal] = type(e).__name__
            finally:
                url = None
    finally:
        if con is not None:
            con.close()
    if not dry_run:
        try:
            from . import events
            events.log("calendar", action="fetch", calendars=len(out["calendars"]), errors=len(out["errors"]),
                       events=sum(c["events"] for c in out["calendars"]))
        except Exception:  # noqa: BLE001
            pass
    return out


# ---- heads-up duty -------------------------------------------------------------------------------------------

def due_headsups(occs, now, lead_min, sent=()):
    """Timed events starting within the lead time (and not yet started) that have no heads-up yet:
    [(occurrence, text)], soonest first."""
    tz = local_tz()
    out = []
    for o in sorted(occs, key=lambda x: x["start"]):
        if o["all_day"] or o["start"] <= now or o["start"] - now > lead_min * 60 or o["ref"] in sent:
            continue
        out.append((o, headsup_text(o, now, tz)))
    return out


def _when(s, o, now):
    """"now" or "wait" for a due heads-up. The heads-up is always an asked-for text (never held, never counted against
    the daily cap), so quiet hours are decided here: an event that itself falls in quiet hours gets it at once; an
    event outside quiet hours whose heads-up moment is inside them waits (the next 5-min tick re-checks, so the text
    says the true minutes left) unless quiet hours would end less than 5 min before it starts."""
    from . import notify
    if notify.in_quiet_hours(s, o["start"]) or not notify.in_quiet_hours(s, now):
        return "now"
    return "now" if notify.quiet_ends(s, now) > o["start"] - 300 else "wait"


def send_headsups(settings=None, now=None):
    """Queue the heads-ups that are due (sqlite only, no network). Exactly once per event start."""
    from . import mailwatch, notify
    s = settings or Settings.load()
    now = time.time() if now is None else now
    lead = lead_minutes()
    if lead <= 0 or not data_path(DB_NAME).exists():
        return {"sent": 0, "disabled": lead <= 0}
    con = _db()
    out = {"sent": 0, "texts": [], "errors": 0}
    try:
        rows = con.execute("SELECT * FROM events WHERE all_day=0 AND start_ts>? AND start_ts<=? ORDER BY start_ts",
                           (now, now + lead * 60)).fetchall()
        occs = [{**dict(r), "start": r["start_ts"], "end": r["end_ts"], "all_day": False,
                 "ref": f"{r['cal']}:{r['uid']}:{int(r['start_ts'])}"} for r in rows]
        sent = {r[0] for r in con.execute("SELECT key FROM headsup WHERE start_ts>?", (now,)).fetchall()}
        for o, text in due_headsups(occs, now, lead, sent):
            if _when(s, o, now) == "wait":
                continue
            try:
                row = notify.deliver("text", text, asked=True, origin=PURPOSE, ref=o["ref"],
                                     topic=f"calendar:{o['ref']}", chat=mailwatch.owner_chat(s), settings=s, now=now,
                                     expires=o["start"] + HEADSUP_GRACE_S)
            except Exception:  # noqa: BLE001  (retried on the next tick; the outbox key keeps it single)
                out["errors"] += 1
                continue
            con.execute("INSERT OR IGNORE INTO headsup VALUES (?,?,?,?,?,?)",
                        (o["ref"], o["cal"], o["uid"], o["start_ts"], now, row.get("id")))
            out["sent"] += 1
            out["texts"].append(text)
        con.execute("DELETE FROM headsup WHERE start_ts<?", (now - 7 * 86400,))
    finally:
        con.close()
    if out["sent"]:
        try:
            from . import events
            events.log("calendar", action="headsup", count=out["sent"])
        except Exception:  # noqa: BLE001
            pass
    return out


# ---- core jobs and timers ------------------------------------------------------------------------------------

def fetch_job(payload=None, *, settings=None, now=None, fetch=None):
    """Job calendar.fetch (detached): read the calendars, then send any heads-up that is now due. A failed fetch is
    counted, not raised (no instant retry; the next run is 15 min away)."""
    out = run_fetch(settings=settings, now=now, fetch=fetch)
    if "skipped" not in out:
        out["headsup"] = send_headsups(settings=settings, now=now)
    return out


def headsup_job(payload=None, *, settings=None, now=None):
    """Job calendar.headsup (in the core, quick): the due heads-ups from the store."""
    return send_headsups(settings=settings, now=now)


HANDLERS = {FETCH_JOB: fetch_job, HEADSUP_JOB: headsup_job}


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s)."""
    return [(FETCH_TIMER, "cron", FETCH_CRON, FETCH_JOB, "latest", FETCH_MAX_LATE_S),
            (HEADSUP_TIMER, "cron", HEADSUP_CRON, HEADSUP_JOB, "skip", None)]


def register_jobs():
    from waggle.core import jobs
    jobs.register(FETCH_JOB, fetch_job, detached=True, max_attempts=2, lease_s=120, timeout_s=300)
    jobs.register(HEADSUP_JOB, headsup_job, max_attempts=1, lease_s=60)
    return list(HANDLERS)


def register_core(now=None):
    """Called by the core process at every start (idempotent). Returns the job kinds and timer ids."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late in timer_specs():
        timers.add(tid, kind, spec, job_kind, catch_up=policy, max_late_s=max_late, origin="calendar", now=now)
        report["timers"].append(tid)
    return report


# ---- status and command line --------------------------------------------------------------------------------

def _iso(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes") if ts else None


def status(settings=None, now=None):
    """Each calendar (whether an address is stored, never the address), the last good fetch, failures in a row, the
    last error class, the time-zone fallbacks used, and the heads-up lead."""
    now = time.time() if now is None else now
    names = calendars()
    out = {"calendars": [], "headsup_min": lead_minutes(), "fetch": "every 15 min, 06:00-22:45",
           "tz": type(local_tz()).__name__}
    con = _read_only()
    try:
        for n in names or []:
            row = _feed(con, n) if con else None
            out["calendars"].append({"name": n, "address_stored": True,
                                     "last_ok": _iso(row["ok_at"]) if row else None,
                                     "events_kept": row["n_events"] if row else 0,
                                     "failed_in_a_row": row["fails"] if row else 0,
                                     "last_error": (row["error"] or None) if row else None})
        if con:
            out["tz_notes"] = sorted({x for r in con.execute("SELECT notes FROM feeds").fetchall()
                                      for x in (r[0] or "").split("; ") if x})
            out["headsups_sent"] = con.execute("SELECT COUNT(*) FROM headsup").fetchone()[0]
    finally:
        if con:
            con.close()
    if not names:
        out["note"] = "no calendar set up: run python -m waggle.calendar_watch login"
    return out


def login(ask_user=input, ask_secret=getpass.getpass, fetch=None):
    """Ask for a calendar name and its secret iCal address (hidden), check that it fetches and parses, store the
    address in Credential Manager ("waggle-calendar-ics:<name>") and the NAME (never the address) in config.json
    `calendars`; then read it once so `today` works at once. Returns (code, message); the message never has the
    address in it."""
    name = str(ask_user("Calendar name (e.g. personal, school) [personal]: ") or "").strip().lower() or "personal"
    if not _NAME.fullmatch(name):
        return 1, "the name may use a-z, 0-9, - and _ only"
    raw = ask_secret("Secret address in iCal format (input hidden; paste it and press Enter): ")
    try:
        url = normalize_url(raw)
    except CalError:
        return 1, "that is not a web address: it should start with https:// (or webcal://)"
    finally:
        raw = None
    now = time.time()
    try:
        _, body, etag, lm = (fetch or FETCH)(url, "", "")
        occs, ctx = _expand(body or "", now)
    except CalError as e:
        return 1, f"could not read the calendar: {reason(e.kind)}"
    except Exception:  # noqa: BLE001
        return 1, "could not read the calendar: the address did not return a readable calendar"
    try:
        wincred.write(CRED_PREFIX + name, name, url)
    except Exception as e:  # noqa: BLE001
        return 1, f"could not store it in Credential Manager ({type(e).__name__})"
    finally:
        url = None
    kept = [c for c in (_config("calendars", []) or [])
            if str((c.get("name") if isinstance(c, dict) else c) or "").lower() != name]
    _set_config("calendars", kept + [{"name": name}])
    con = _db()
    try:
        _store_events(con, name, occs)
        _save_feed(con, name, etag=etag or "", last_modified=lm or "", body=body, fetched_at=now, ok_at=now, fails=0,
                   error="", n_events=len(occs), notes="; ".join(sorted(ctx.notes)))
    finally:
        con.close()
    return 0, f"ok: read calendar [{name}], {len(occs)} events in the next {WINDOW_DAYS} days"


def _print_today():
    tz = local_tz()
    items = today()
    if not configured():
        print("No calendar set up yet: run python -m waggle.calendar_watch login")
        return 1
    ok = last_ok()
    print(f"Today, {dt.datetime.now(tz):%A %b %d} (calendar last read "
          f"{dt.datetime.fromtimestamp(ok, tz):%a %I:%M %p})" if ok else "Today (not read yet: run `run`)")
    for o in items:
        print("  " + event_line(o, tz))
    if not items:
        print("  no events")
    return 0


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="python -m waggle.calendar_watch", description="Waggle's calendar watch")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="calendars, last good read, errors, time-zone notes")
    sub.add_parser("login", help="add or update a calendar (its secret iCal address, kept in Credential Manager)")
    sub.add_parser("today", help="today's events from the store")
    run = sub.add_parser("run", help="read the calendar now and send due heads-ups")
    run.add_argument("--dry-run", action="store_true", help="read the real calendar, print what would be sent, "
                                                            "write and send nothing")
    args = p.parse_args(argv)
    try:  # texts may carry non-ASCII; a piped Windows console would otherwise be cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if args.cmd == "login":
        code, message = login()
        print(message)
        return code
    if args.cmd == "today":
        return _print_today()
    if args.cmd == "run":
        now = time.time()
        if args.dry_run:
            out = run_fetch(now=now, dry_run=True)
            tz, lead = local_tz(), lead_minutes()
            for c in out.get("calendars", []):
                occs = c.pop("occurrences", [])
                for o in occs:
                    o["ref"] = o["key"]
                print(f"[{c['calendar']}] next events:")
                for o in [x for x in occs if x["end"] > now][:8]:
                    print("  " + dt.datetime.fromtimestamp(o["start"], tz).strftime("%a %b %d") + "  " +
                          event_line(o, tz))
                for o, text in due_headsups(occs, now, lead):
                    print("WOULD SEND:\n" + text + "\n")
            print(json.dumps(out, indent=1, ensure_ascii=False))
            return 1 if out.get("errors") else 0
        out = run_fetch(now=now, force=True)
        if "skipped" not in out:
            out["headsup"] = send_headsups(now=now)
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 1 if out.get("errors") else 0
    print(json.dumps(status(), indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
