"""What the world's calendar says about a date: the "Holidays and observances" list of that date's Wikipedia page.

A sense for the thinking pass (waggle/think.py), nothing more: it lists what is observed on a date and leaves it
to the model whether any of it has to do with the owner. A model asked from memory names the big days and misses
the small ones, so the list is fetched (two small API requests per date) and kept in ~/.waggle/worldcal.json,
keyed by month-day, refreshed after REFRESH_DAYS. Days that move every year (the second Sunday of a month, the
day the clocks change) are not on these pages; the model has to know those.

No model call, no key, nothing about the owner is sent: the request names only a month and a day.
Any failure gives an empty list and is retried on a later pass.
"""
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request

from .config import data_path

API = "https://en.wikipedia.org/w/api.php"
AGENT = "WaggleAssistant/1.0 (date lookup)"
REFRESH_DAYS = 180
RETRY_S = 6 * 3600        # after a failed fetch
MAX_ITEMS = 16
SECTION = re.compile(r"holidays?\s+and\s+observances", re.I)


def _get(params, timeout=12):
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https address
        return json.loads(r.read().decode("utf-8", "replace"))


def page_name(day):
    return f"{day:%B}_{day.day}"


def _plain(line):
    line = re.sub(r"<ref[^>]*/>", "", line)
    line = re.sub(r"<ref[^>]*>.*?</ref>", "", line, flags=re.S)
    line = re.sub(r"<ref[^>]*>.*", "", line, flags=re.S)       # a reference cut off by the end of the line
    for _ in range(3):
        line = re.sub(r"\{\{[^{}]*\}\}", "", line)
    line = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", line)
    line = re.sub(r"<[^>]+>", "", line).replace("'''", "").replace("''", "")
    return " ".join(line.split()).strip(" :;,")


def parse(wikitext):
    """The top-level entries of the section, as plain text. Sub-entries (the lists of feast days) are left out, and
    so is a top-level line that only introduces them."""
    out = []
    lines = wikitext.splitlines()
    for i, raw in enumerate(lines):
        l = raw.strip()
        if not l.startswith("*") or l.startswith("**"):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        text = _plain(l.lstrip("*"))
        if not text or (nxt.startswith("**") and raw.rstrip().endswith(":")):
            continue
        if text not in out:
            out.append(text[:120])
    return out[:MAX_ITEMS]


def _load():
    try:
        return json.loads(data_path("worldcal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(cache):
    try:
        data_path("worldcal.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def observances(day, get=None, now=None):
    """What is observed on `day` (a date): a list of short strings, [] when unknown."""
    now = now or time.time()
    key = f"{day.month:02d}-{day.day:02d}"
    cache = _load()
    hit = cache.get(key)
    if hit and hit.get("items") is not None and now - hit.get("fetched", 0) < REFRESH_DAYS * 86400:
        return list(hit["items"])
    if hit and hit.get("failed") and now - hit["failed"] < RETRY_S:
        return list(hit.get("old") or [])
    get = get or _get
    try:
        secs = get({"action": "parse", "page": page_name(day), "prop": "sections"})["parse"]["sections"]
        idx = next(s["index"] for s in secs if SECTION.search(s.get("line", "")))
        text = get({"action": "parse", "page": page_name(day), "prop": "wikitext", "section": idx})["parse"]["wikitext"]
        items = parse(text)
        cache[key] = {"fetched": now, "items": items}
    except Exception:  # noqa: BLE001 - offline, the page changed shape, anything: no list this time
        cache[key] = {"failed": now, "old": (hit or {}).get("items") or (hit or {}).get("old") or []}
        items = list(cache[key]["old"])
    _save(cache)
    return items


def lines(now=None, days=3, get=None):
    """'Thu 2026-11-19: a; b; c' for today and the days after it (days in all); a date with no list is left out."""
    today = dt.date.fromtimestamp(now or time.time())
    out = []
    for i in range(days):
        d = today + dt.timedelta(days=i)
        items = observances(d, get=get, now=now)
        if items:
            out.append(f"{d:%a} {d:%Y-%m-%d}: " + "; ".join(items))
    return out
