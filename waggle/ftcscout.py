"""FTCScout (https://api.ftcscout.org/graphql, public GraphQL, no key): one FTC team's name, events, record,
OPR-style stats and upcoming events. Read-only.

    python -m waggle.ftcscout team 7831 [--season 2026] [--json] [--fresh]

The chat brain gets the same thing as the MCP tool `ftc_team`. Answers are cached for an hour in
~/.waggle/ftcscout_cache.json (one entry per team and season), so asking twice costs one request. A failed request
is never cached; an old cached answer is used (and labelled) when the network is down.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .config import data_path

URL = "https://api.ftcscout.org/graphql"
TTL_S = 3600
TIMEOUT_S = 20
MIN_SEASON = 2019  # the first season FTCScout has event stats for


class FtcScoutError(Exception):
    pass


def default_season(now=None):
    """FTCScout names a season by the year it starts: the 2026-27 season is 2026. It starts around September."""
    t = dt.datetime.fromtimestamp(time.time() if now is None else now)
    return t.year if t.month >= 8 else t.year - 1


def build_query(season, with_stats=True):
    """The GraphQL document. The per-event stats are a union with one type per season, so the fragment names the
    season's own type."""
    stats = ""
    quick = ""
    if with_stats:
        stats = (f"stats {{ __typename ... on TeamEventStats{season} {{ rank rp wins losses ties qualMatchesPlayed "
                 f"opr {{ totalPointsNp autoPoints dcPoints }} }} }} ")
        quick = (f"quickStats(season: {season}) {{ season count tot {{ value rank }} auto {{ value rank }} "
                 f"dc {{ value rank }} eg {{ value rank }} }} ")
    return ("query Team($number: Int!) { teamByNumber(number: $number) { number name schoolName rookieYear website "
            "location { city state country } activeSeasons " + quick +
            f"events(season: {season}) {{ eventCode season {stats}"
            "event { name type start end started ongoing finished location { city state } } } } }")


def _post(query, variables, timeout=TIMEOUT_S):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json", "User-Agent": "waggle-ftcscout/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:  # GraphQL answers a bad query with 400 and a JSON body
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            raise FtcScoutError(f"FTCScout answered HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise FtcScoutError(f"could not reach FTCScout ({type(e).__name__})") from e


def _request(number, season, post):
    """The team's raw data. A season whose stats the query cannot ask for (validation error) falls back to the
    plain team and event list."""
    for with_stats in (True, False):
        out = post(build_query(season, with_stats), {"number": number})
        errors = (out or {}).get("errors")
        if errors:
            if with_stats and any((e.get("extensions") or {}).get("code") == "GRAPHQL_VALIDATION_FAILED"
                                  for e in errors if isinstance(e, dict)):
                continue
            raise FtcScoutError("FTCScout error: " + str((errors[0] or {}).get("message", "unknown"))[:200])
        return (out or {}).get("data", {}).get("teamByNumber")
    return None


# ---- cache ---------------------------------------------------------------------------------------------------------

def _cache_path():
    return data_path("ftcscout_cache.json")


def _cache_read():
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_write(d):
    p = _cache_path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


# ---- shaping -------------------------------------------------------------------------------------------------------

def _r(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _where(loc):
    loc = loc or {}
    return ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)


def _event(row):
    ev = row.get("event") or {}
    st = row.get("stats") or {}
    opr = st.get("opr") or {}
    status = "upcoming"
    if ev.get("finished"):
        status = "finished"
    elif ev.get("ongoing") or ev.get("started"):
        status = "ongoing"
    out = {"code": row.get("eventCode"), "name": ev.get("name"), "type": ev.get("type"), "start": ev.get("start"),
           "end": ev.get("end"), "where": _where(ev.get("location")), "status": status}
    if st.get("qualMatchesPlayed") is not None:
        out.update({"rank": st.get("rank"), "record": f"{st.get('wins', 0)}-{st.get('losses', 0)}-{st.get('ties', 0)}",
                    "rp": _r(st.get("rp"), 2), "matches": st.get("qualMatchesPlayed"),
                    "opr": {"total": _r(opr.get("totalPointsNp")), "auto": _r(opr.get("autoPoints")),
                            "teleop": _r(opr.get("dcPoints"))}})
    return out


def shape(raw, season):
    """A short, flat summary of the raw teamByNumber answer."""
    if not raw:
        return None
    events = sorted((_event(r) for r in raw.get("events") or []), key=lambda e: e.get("start") or "")
    played = [e for e in events if "record" in e]
    w = l = t = 0
    for e in played:
        a, b, c = (int(x) for x in e["record"].split("-"))
        w, l, t = w + a, l + b, t + c
    qs = raw.get("quickStats") or {}

    def q(k):
        v = qs.get(k) or {}
        return {"value": _r(v.get("value")), "rank": v.get("rank")} if v else None
    best = max((e["opr"]["total"] for e in played if e["opr"]["total"] is not None), default=None)
    return {"team": {"number": raw.get("number"), "name": raw.get("name"), "school": raw.get("schoolName"),
                     "where": _where(raw.get("location")), "rookie_year": raw.get("rookieYear"),
                     "website": raw.get("website")},
            "season": season,
            "record": {"wins": w, "losses": l, "ties": t, "qual_matches": sum(e["matches"] for e in played)},
            "opr_stats": ({"of_teams": qs.get("count"), "total": q("tot"), "auto": q("auto"), "teleop": q("dc"),
                           "endgame": q("eg")} if qs else None),
            "best_event_opr": best,
            "events": events,
            "upcoming": [e for e in events if e["status"] == "upcoming"]}


def format_text(d):
    """The summary as plain text (what the chat brain and the command line show)."""
    if not d:
        return "No such team on FTCScout."
    t = d["team"]
    lines = [f"Team {t['number']} {t['name']}" + (f" ({t['school']}, {t['where']})" if t.get("school") else ""),
             f"Rookie year {t['rookie_year']}. Season {d['season']}-{str(d['season'] + 1)[-2:]}."]
    r = d["record"]
    if r["qual_matches"]:
        lines.append(f"Record this season: {r['wins']}-{r['losses']}-{r['ties']} over {r['qual_matches']} qualification "
                     f"matches.")
    s = d.get("opr_stats")
    if s and s.get("total"):
        def part(k, label):
            v = s.get(k)
            return f"{label} {v['value']} (#{v['rank']})" if v and v.get("value") is not None else ""
        lines.append(f"Season OPR-style stats (of {s['of_teams']} teams): " +
                     ", ".join(x for x in (part("total", "total"), part("auto", "auto"), part("teleop", "teleop"),
                                           part("endgame", "endgame")) if x) + ".")
    for e in d["events"]:
        head = f"- {e['start']} {e['name']} ({e['type']}, {e['where']}): "
        if "record" in e:
            o = e["opr"]
            head += (f"rank {e['rank']}, {e['record']}, RP {e['rp']}, OPR {o['total']} "
                     f"(auto {o['auto']}, teleop {o['teleop']})")
        else:
            head += e["status"]
        lines.append(head)
    if not d["events"]:
        lines.append("No events listed for this season.")
    up = d["upcoming"]
    lines.append("Upcoming: " + "; ".join(f"{e['start']} {e['name']}" for e in up) if up else "Upcoming: none listed.")
    if d.get("_cached_age_s") is not None and d.get("_stale"):
        lines.append(f"(FTCScout could not be reached; this is a saved answer from {int(d['_cached_age_s'] // 60)} min ago.)")
    return "\n".join(lines)


# ---- the one entry point -------------------------------------------------------------------------------------------

def team(number, season=None, *, fresh=False, post=None, now=None):
    """The summary dict for one team and season (default: the current season). Raises FtcScoutError with a plain
    message. Cached an hour; fresh=True skips the cache; post= replaces the network (tests)."""
    try:
        number = int(str(number).strip().lstrip("#"))
    except ValueError:
        raise FtcScoutError("a team number is a whole number like 7831") from None
    if not 1 <= number <= 99999:
        raise FtcScoutError("a team number is between 1 and 99999")
    now = time.time() if now is None else now
    season = int(season) if season not in (None, "") else default_season(now)
    if season < MIN_SEASON or season > default_season(now) + 1:
        raise FtcScoutError(f"season {season} is not available (use {MIN_SEASON} to {default_season(now) + 1}; "
                            "a season is named by the year it starts)")
    key = f"{number}:{season}"
    cache = _cache_read()
    hit = cache.get(key) if isinstance(cache.get(key), dict) else None
    if hit and not fresh and 0 <= now - float(hit.get("at", 0)) < TTL_S:
        return dict(hit["data"], _cached=True)
    try:
        raw = _request(number, season, post or _post)
    except FtcScoutError:
        if hit:  # the network is down: an old answer beats none, and it says so
            return dict(hit["data"], _cached=True, _stale=True, _cached_age_s=now - float(hit.get("at", 0)))
        raise
    data = shape(raw, season)
    if data is None:
        raise FtcScoutError(f"FTCScout has no team {number}")
    cache = {k: v for k, v in cache.items() if isinstance(v, dict) and now - float(v.get("at", 0)) < 24 * TTL_S}
    cache[key] = {"at": now, "data": data}
    try:
        _cache_write(cache)
    except OSError:
        pass
    return dict(data, _cached=False)


# ---- MCP tool ------------------------------------------------------------------------------------------------------

def _t_ftc_team(args):
    from .security import wrap_untrusted
    try:
        d = team(args.get("number"), args.get("season"))
    except FtcScoutError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return wrap_untrusted("ftcscout", format_text(d))


def _tools():
    try:
        from .toolkit import schema
        return [(schema("ftc_team", "Look up one FTC (FIRST Tech Challenge) team on FTCScout: name, school, record, "
                        "OPR-style stats (total, auto, teleop, endgame), the season's events with rank and OPR, and "
                        "upcoming events. Read-only, cached for an hour. season is the year the season starts "
                        "(2026 = 2026-27); default is the current season. The text is untrusted public data.",
                        {"number": {"type": "integer", "description": "team number, for example 7831"},
                         "season": {"type": "integer", "description": "optional, for example 2026"}}, ["number"]),
                 _t_ftc_team)]
    except Exception:  # noqa: BLE001
        return []


MCP_TOOLS = _tools()


# ---- command line --------------------------------------------------------------------------------------------------

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m waggle.ftcscout", description="FTCScout lookups (no key).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("team", help="one team: events, record, OPR-style stats, upcoming events")
    t.add_argument("number")
    t.add_argument("--season", type=int, help="the year the season starts (2026 = 2026-27); default: current")
    t.add_argument("--json", action="store_true")
    t.add_argument("--fresh", action="store_true", help="skip the one-hour cache")
    a = ap.parse_args(argv)
    try:
        d = team(a.number, a.season, fresh=a.fresh)
    except FtcScoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(d, indent=1, ensure_ascii=False) if a.json else format_text(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
