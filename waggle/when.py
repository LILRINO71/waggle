"""Plain-English times -> datetimes: 'at 7', '7:30pm', 'tomorrow', 'tonight', 'friday', 'in 2 hours', '2026-11-21'.

parse(text, now) -> (datetime or None, has_time). No model, no network: used by follow-ups, one-shot reminders and
"call me at 7". It only reads explicit time words, so "a 312 RPM motor" or "3 commits" never become a time.
"""
import datetime as dt
import re

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
            "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                          "september", "october", "november", "december"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
PARTS = {"morning": 9, "noon": 12, "midday": 12, "lunch": 12, "afternoon": 15, "evening": 19, "tonight": 20,
         "night": 20, "midnight": 0}

_IN = re.compile(r"\bin\s+(an?|one|two|three|half an?|\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|h|days?)\b", re.I)
_AT = re.compile(r"\b(?:at|by|around|@)\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d:])", re.I)
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?\b", re.I)
_AMPM = re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", re.I)
_ISO = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
_MONTHDAY = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                       re.I)
_WEEKDAY = re.compile(r"\b(next\s+|this\s+|on\s+)?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b", re.I)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def _hour(h, minute, ampm):
    h, minute = int(h), int(minute or 0)
    if h > 23 or minute > 59:
        return None
    if ampm:
        pm = ampm.lower().startswith("p")
        if h == 12:
            h = 12 if pm else 0
        elif pm:
            h += 12
    return h, minute


def _day(text, now):
    """(date, explicit) for a day word in text, or (None, False)."""
    low = text.lower()
    m = _ISO.search(low)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), True
        except ValueError:
            pass
    m = _MONTHDAY.search(low)
    if m:
        try:
            d = dt.date(now.year, MONTHS[m.group(1).lower()], int(m.group(2)))
            if d < now.date() - dt.timedelta(days=30):
                d = d.replace(year=now.year + 1)
            return d, True
        except ValueError:
            pass
    if re.search(r"\b(?:day after tomorrow)\b", low):
        return now.date() + dt.timedelta(days=2), True
    if re.search(r"\b(?:tomorrow|tmrw|tmr)\b", low):
        return now.date() + dt.timedelta(days=1), True
    if re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", low):
        return now.date(), True
    if re.search(r"\bthis weekend\b", low):
        ahead = (5 - now.weekday()) % 7
        return now.date() + dt.timedelta(days=ahead), True
    if re.search(r"\bnext week\b", low):
        return now.date() + dt.timedelta(days=7 - now.weekday()), True
    m = _WEEKDAY.search(low)
    if m:
        want = WEEKDAYS[m.group(2)]
        ahead = (want - now.weekday()) % 7
        if ahead == 0 or (m.group(1) or "").strip() == "next":
            ahead = ahead or 7
        return now.date() + dt.timedelta(days=ahead), True
    return None, False


def _clock(text):
    """(hour, minute, ambiguous) for an explicit clock time in text, or None."""
    low = text.lower()
    for rx in (_CLOCK, _AMPM, _AT):
        m = rx.search(low)
        if not m:
            continue
        if rx is _AMPM:
            hm = _hour(m.group(1), 0, m.group(2))
            ampm = m.group(2)
        else:
            hm = _hour(m.group(1), m.group(2), m.group(3))
            ampm = m.group(3)
        if hm:
            return hm[0], hm[1], not ampm and hm[0] <= 12
    for word, h in PARTS.items():
        if re.search(rf"\b{word}\b", low):
            return h, 0, False
    return None


def parse(text, now=None):
    """(datetime, has_time) or (None, False). 'at 7' with no am/pm picks the next sensible 7 (7 am only if it is
    still ahead today, else 7 pm); 1-6 with no am/pm mean the afternoon."""
    now = now or dt.datetime.now()
    text = text or ""
    m = _IN.search(text)
    if m:
        n_raw, unit = m.group(1).lower(), m.group(2).lower()
        n = 0.5 if n_raw.startswith("half") else _WORDS.get(n_raw) or float(n_raw)
        secs = n * (60 if unit.startswith("m") else 86400 if unit.startswith("d") else 3600)
        return now + dt.timedelta(seconds=secs), not unit.startswith("d")
    day, explicit = _day(text, now)
    clock = _clock(text)
    if clock is None:
        return (dt.datetime.combine(day, dt.time(0, 0)), False) if day else (None, False)
    h, minute, ambiguous = clock
    if ambiguous and 1 <= h <= 6:
        h += 12
    base = day or now.date()
    when = dt.datetime.combine(base, dt.time(h, minute))
    if ambiguous and h < 12 and (when <= now or re.search(r"\b(tonight|evening)\b", text.lower())):
        when += dt.timedelta(hours=12)
    if not explicit and when <= now:
        when += dt.timedelta(days=1)
    return when, True


# ---- date ranges for questions about the past ('what did we decide last week') ----------------------------------

_AGO = re.compile(r"\b(\d{1,3}|a|an|one|two|three|four|five|six|seven)\s+(day|week|month)s?\s+ago\b", re.I)
_AGO_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_LAST_DAY = re.compile(r"\blast\s+(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b", re.I)
_IN_MONTH = re.compile(r"\b(?:in|during|last|this|early|late|back in)\s+(" + "|".join(
    sorted((m for m in MONTHS if len(m) > 3), key=len, reverse=True)) + r")\b", re.I)


def range_for(text, now=None):
    """(first_day, last_day) the question is about, as dates (both inclusive), or None when it names no day or
    period. Reads past and present words that parse() leaves out: yesterday, last week, 3 days ago, last Friday, in
    October, recently. A single day gives (day, day). No model."""
    now = now or dt.datetime.now()
    today = now.date()
    low = (text or "").lower()
    one = dt.timedelta(days=1)
    m = _ISO.search(low)
    if m:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d
        except ValueError:
            pass
    if re.search(r"\bday before yesterday\b", low):
        return today - 2 * one, today - 2 * one
    if re.search(r"\b(yesterday|last night)\b", low):
        return today - one, today - one
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening)|earlier today)\b", low):
        return today, today
    m = _AGO.search(low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _AGO_NUM[m.group(1).lower()]
        unit = m.group(2).lower()
        days = n * (1 if unit == "day" else 7 if unit == "week" else 30)
        d = today - days * one
        return (d, d) if unit == "day" else (d - 3 * one, d + 3 * one)
    if re.search(r"\blast week\b", low):
        start = today - dt.timedelta(days=today.weekday() + 7)
        return start, start + 6 * one
    if re.search(r"\bthis week\b", low):
        return today - dt.timedelta(days=today.weekday()), today
    if re.search(r"\blast month\b", low):
        end = today.replace(day=1) - one
        return end.replace(day=1), end
    if re.search(r"\bthis month\b", low):
        return today.replace(day=1), today
    if re.search(r"\b(recently|lately|these days|the past few days|past few days)\b", low):
        return today - 7 * one, today
    m = _LAST_DAY.search(low)
    if m:
        ahead = (today.weekday() - WEEKDAYS[m.group(1).lower()]) % 7 or 7
        d = today - ahead * one
        return d, d
    m = _MONTHDAY.search(low)
    if m:
        try:
            d = dt.date(today.year, MONTHS[m.group(1).lower()], int(m.group(2)))
            if d > today + dt.timedelta(days=200):
                d = d.replace(year=today.year - 1)
            return d, d
        except ValueError:
            pass
    m = _IN_MONTH.search(low)
    if m:
        month = MONTHS[m.group(1).lower()]
        year = today.year if month <= today.month else today.year - 1
        first = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return first, nxt - one
    if re.search(r"\btomorrow\b", low):
        return today + one, today + one
    return None
