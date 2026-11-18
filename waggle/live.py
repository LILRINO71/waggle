"""Live information the local model can't know: weather (Open-Meteo, free, no key).

Only the city name goes to Open-Meteo; nothing else about the user leaves the PC."""
import datetime as dt
import json
import re
import urllib.parse
import urllib.request

from .config import Settings

LIVE_HINT = re.compile(r"\b(weather|forecast|rain|snow|temperature|news|score|scores|price|stock|today|tomorrow|"
                       r"tonight|this week|latest|right now|currently|open now|traffic)\b", re.I)
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers",
       86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms"}


def needs_live_info(text):
    return bool(LIVE_HINT.search(text or ""))


def _get(url, timeout=15, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "waggle-agent"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (OSError, TimeoutError):
            if attempt == tries - 1:
                raise


def geocode(place, fetch=None):
    fetch = fetch or _get
    q = urllib.parse.urlencode({"name": place, "count": 1, "language": "en", "format": "json"})
    res = (fetch(f"https://geocoding-api.open-meteo.com/v1/search?{q}") or {}).get("results") or []
    if not res:
        return None
    r = res[0]
    label = ", ".join(x for x in [r.get("name"), r.get("admin1"), r.get("country_code")] if x)
    return {"lat": r["latitude"], "lon": r["longitude"], "label": label, "tz": r.get("timezone", "auto")}


def weather(place=None, day="tomorrow", settings=None, fetch=None, today=None):
    """Plain-text forecast for today/tomorrow/next 3 days. Saves `place` as home the first time it's given."""
    s = settings or Settings.load()
    if place and not s.home_location:
        s.home_location = place
        s.save()
    place = place or s.home_location
    if not place:
        return "NO_LOCATION: ask the user which city they want the weather for (it will be remembered)."
    imperial = s.units == "imperial"
    # wttr.in first: api.open-meteo.com is unreachable on this PC's network (handshake times out).
    try:
        return _wttr(place, day, imperial, fetch, today)
    except (OSError, KeyError, ValueError, IndexError, TimeoutError):
        pass
    geo = geocode(place, fetch)
    if not geo:
        return f"Couldn't find a place called '{place}'. Ask the user for the city and state/country."
    return _open_meteo(geo, day, imperial, fetch, today)


def _open_meteo(geo, day, imperial, fetch=None, today=None):
    q = urllib.parse.urlencode({
        "latitude": geo["lat"], "longitude": geo["lon"], "timezone": geo["tz"], "forecast_days": 4,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh"})
    d = (fetch or _get)(f"https://api.open-meteo.com/v1/forecast?{q}")["daily"]
    today = today or dt.date.today()
    want = {"today": [0], "tomorrow": [1], "week": [0, 1, 2, 3]}.get(day, [1])
    t, w = ("°F", "mph") if imperial else ("°C", "km/h")
    lines = [f"Weather for {geo['label']}:"]
    for i in want:
        date = dt.date.fromisoformat(d["time"][i])
        name = "Today" if date == today else "Tomorrow" if date == today + dt.timedelta(days=1) else f"{date:%A}"
        lines.append(f"- {name}: {WMO.get(d['weather_code'][i], 'mixed')}, high {round(d['temperature_2m_max'][i])}{t}, "
                     f"low {round(d['temperature_2m_min'][i])}{t}, {d['precipitation_probability_max'][i] or 0}% "
                     f"chance of rain/snow, wind up to {round(d['wind_speed_10m_max'][i])} {w}")
    return "\n".join(lines)


def _wttr(place, day, imperial, fetch=None, today=None):
    data = (fetch or _get)(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1")
    area = (data.get("nearest_area") or [{}])[0]
    label = ", ".join(x[0]["value"] for x in (area.get("areaName"), area.get("region"), area.get("country")) if x)
    days = data["weather"]
    today = today or dt.date.today()
    want = {"today": [0], "tomorrow": [1], "week": [0, 1, 2]}.get(day, [1])
    t, w = ("°F", "mph") if imperial else ("°C", "km/h")
    lines = [f"Weather for {label or place}:"]
    for i in want:
        if i >= len(days):
            continue
        dd = days[i]
        date = dt.date.fromisoformat(dd["date"])
        name = "Today" if date == today else "Tomorrow" if date == today + dt.timedelta(days=1) else f"{date:%A}"
        hours = dd.get("hourly") or []
        desc = hours[len(hours) // 2]["weatherDesc"][0]["value"].strip().lower() if hours else "mixed"
        rain = max([int(h.get("chanceofrain", 0)) for h in hours] + [int(h.get("chanceofsnow", 0)) for h in hours] + [0])
        wind = max([int(h.get("windspeedMiles" if imperial else "windspeedKmph", 0)) for h in hours] + [0])
        hi, lo = (dd["maxtempF"], dd["mintempF"]) if imperial else (dd["maxtempC"], dd["mintempC"])
        lines.append(f"- {name}: {desc}, high {hi}{t}, low {lo}{t}, {rain}% chance of rain/snow, wind up to {wind} {w}")
    return "\n".join(lines)


WEATHER_HINT = re.compile(r"\b(weather|forecast|rain|snow|temperature|temp|hot|cold|umbrella|jacket)\b", re.I)
# Lookahead capture so matches can overlap: "for tomorrow in Chicago" must still try "in Chicago".
CITY = re.compile(r"\b(?:in|for|at)\s+(?=([A-Za-z][A-Za-z .,'-]{1,50}))", re.I)
TIME_TAIL = re.compile(r"\s*\b(today|tomorrow|tonight|this (?:week|weekend|morning|afternoon|evening)|"
                       r"next (?:week|few days)|right now|now|the (?:morning|afternoon|evening|weekend))\b.*$", re.I)
NOT_CITY = {"a bit", "a week", "general", "a while", "me", "us"}


def weather_request(text):
    """(city or None, day) if the message is about weather, else None."""
    if not WEATHER_HINT.search(text or ""):
        return None
    low = text.lower()
    day = "week" if re.search(r"\b(week|weekend|next few days)\b", low) else \
          "today" if re.search(r"\b(today|tonight|right now|now)\b", low) else "tomorrow"
    city = None
    for m in CITY.finditer(text):
        cand = TIME_TAIL.sub("", m.group(1))
        cand = re.split(r"\s+\b(?:in|for|at)\b\s+", cand, maxsplit=1, flags=re.I)[0].strip(" ,.?!")
        if cand and cand.lower() not in NOT_CITY:
            city = cand
            break
    return city, day


def prefetch(text, settings=None, search=None):
    """Live data gathered BEFORE the local model answers (it is unreliable at choosing tools).
    Returns a context block or ''. `search` runs Hermes's web_search tool."""
    from .security import wrap_untrusted
    w = weather_request(text)
    if w:
        city, day = w
        try:
            data = weather(city, day, settings)
        except Exception as e:
            data = f"Weather lookup failed ({type(e).__name__}); say so and suggest a weather app."
        if data.startswith("NO_LOCATION"):
            return ("<live_data>The user asked about weather but no city is saved. Ask which city they want; "
                    "it will be remembered.</live_data>")
        return f"<live_data source=\"weather\">\n{data}\nAnswer from this data.</live_data>"
    if search and needs_live_info(text):
        try:
            results = search(text)
        except Exception:
            return ""
        if results:
            return ("<live_data source=\"web_search\">Fresh search results for the user's question. Answer from "
                    "them and mention the source site.\n" + wrap_untrusted("web_search", str(results), 6000)
                    + "\n</live_data>")
    return ""


LIVE_NUDGE = ("This message asks for live or current information. Use your tools: `weather` for forecasts, "
              "`web_search` for news, scores, prices and anything recent. Never say you can't look it up "
              "without trying a tool first.")
