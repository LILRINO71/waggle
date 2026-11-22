"""ActivityWatch as an extra activity sensor: what the owner works on across the whole PC, not only in Claude Code.

ActivityWatch (aw-server on http://127.0.0.1:5600, local only) keeps raw window titles, AFK state and, with the
browser extension, tab URLs. Waggle reads it with stdlib urllib and keeps only DERIVED facts:

  app        the program ("chrome", "code"), lower case, no .exe
  project    a guess from rules: ftc, waggle, school, onshape, laser, cad, coding, notes, ... or a leisure kind
             (video, social, anime, games); empty when nothing matches
  ws         a workstream key (a repository's folder name) when the title names one
  host       the web host, kept ONLY when it matched a rule (never an unknown site)
  tid        a salted hash of the title (counts distinct windows; cannot be looked up in a word list)
  secs       how long, with AFK time cut out; AFK itself is stored as its own rows

Raw titles and URLs live in memory for the length of one pass. They are never stored, never logged, never put in
an event, never sent to Claude. The one place a title may travel is the focus check's local-model question
(waggle/focus.py), which goes to Ollama on this PC.

Derived rows go to autonomy.db (tables aw_slices, aw_state), are pruned after KEEP_DAYS, and feed
  activity.build()   work minutes per workstream (0.5 per 10 focused minutes, 2-day decay), leisure ignored
  miner.mine()       app sequences ("code > chrome:onshape > vls") repeated on several days
The core timer activitywatch.poll runs every 10 minutes and does nothing in quiet hours. Config switch
`activitywatch_enabled` (default true): with ActivityWatch not running, every call here is a quiet no-op.

    poll(...)       read new events, store the derived slices   -> small report, never titles
    slices_since(t) the stored rows
    current(...)    the foreground window right now (in memory only), for the focus check
    classify(...)   (project, ws) from app / title / host
"""
import datetime as dt
import hashlib
import json
import bisect
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import autonomy_store as store_
from .config import Settings

URL = "http://127.0.0.1:5600"
TIMEOUT_S = 4.0
CATCH_UP_S = 24 * 3600      # the first pass, and a pass after a long gap, look back this far
OVERLAP_S = 900             # every pass re-reads this much: events grow while the heartbeat merges them
KEEP_DAYS = 30
MAX_EVENTS = 5000
MIN_SLICE_S = 5
STALE_S = 150               # a "current" window older than this is not current
JOB = "activitywatch.poll"
TIMER = "activitywatch.poll"
EVERY = "10m"

BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "opera_gx", "vivaldi", "arc", "iexplore"}
NOISE_APPS = {"explorer", "searchhost", "searchapp", "lockapp", "shellexperiencehost", "startmenuexperiencehost",
              "applicationframehost", "textinputhost", "taskmgr", "dwm", "sihost", "systemsettings", "logonui",
              "unknown", ""}
PROJECTS = {"ftc", "waggle", "school", "onshape", "laser", "cad"}   # these become workstreams of their own
LEISURE = {"video", "social", "anime", "games"}                      # never work; what focus nudges are about

# (project, regex). Each list is tried in order, first match wins. Titles and hosts are matched lower case.
HOST_RULES = [
    ("onshape", r"(^|\.)onshape\.com$"),
    ("ftc", r"gobilda\.com|firstinspires\.org|ftcscout\.org|andymark\.com|revrobotics\.com|ftc-docs|"
            r"ftcsim|pedropathing|gm0\.org"),
    ("school", r"classroom\.google\.com|desmos\.com|khanacademy\.org|instructure\.com|schoology\.com|"
               r"powerschool|quizlet\.com|wolframalpha\.com|symbolab\.com|geogebra\.org|collegeboard\.org|"
               r"mathway\.com|ixl\.com|\.edu$|studocu|chegg\.com|brilliant\.org"),
    ("anime", r"crunchyroll|hianime|aniwatch|9anime|gogoanime|animepahe|myanimelist|anilist\.co|funimation|"
              r"kissanime|animekai|anime"),
    ("games", r"store\.steampowered|steamcommunity|roblox\.com|poki\.com|crazygames|coolmathgames|itch\.io|"
              r"miniclip|y8\.com|kongregate|epicgames\.com|friv|krunker|geoguessr|chess\.com|lichess|"
              r"gamespot\.com"),
    ("video", r"youtube\.com|youtu\.be|twitch\.tv|netflix\.com|hulu\.com|disneyplus|primevideo|tiktok\.com|"
              r"vimeo\.com|dailymotion"),
    ("social", r"reddit\.com|twitter\.com|(^|\.)x\.com$|instagram\.com|facebook\.com|snapchat|9gag|pinterest|"
               r"tumblr|threads\.net|bsky\.app"),
    ("coding", r"github\.com|gitlab\.com|stackoverflow\.com|developer\.mozilla|docs\.python\.org|pypi\.org|"
               r"npmjs\.com|huggingface\.co|ollama\.com|readthedocs|learn\.microsoft\.com"),
    ("assistant", r"claude\.ai|chatgpt\.com|gemini\.google\.com"),
]
# Specific projects and leisure kinds by title, before the program decides ("TeleOp.java - ftc - VS Code" is ftc).
TITLE_RULES = [
    ("onshape", r"onshape"),
    ("ftc", r"\bftc\b|gobilda|teamcode|first tech|biobuzz|simbench|ftcscout|rev robotics|andymark|"
            r"driver hub|robot controller|titan f1"),
    ("waggle", r"waggle|hermes agent|activitywatch"),
    ("laser", r"laser station|laser pipeline|\bvls\b|universal laser|laserwork"),
    ("school", r"calculus|algebra|geometry homework|trigonometry|math 1\d{3}|homework|worksheet|study guide|"
              r"\bexam\b|\bquiz\b|essay|chemistry|physics|biology|ap \w+ |unit \d+ (test|review)|\bsyllabus\b|"
              r"khan academy|desmos|google classroom"),
    ("anime", r"\banime\b|crunchyroll|hianime|aniwatch|myanimelist|\bepisode \d+\b.*\b(sub|dub|english)\b"),
    ("games", r"minecraft|roblox|fortnite|valorant|league of legends|genshin|\bsteam\b|geometry dash|"
              r"among us|brawl stars|rocket league|counter-strike|apex legends|overwatch|call of duty|"
              r"poki|crazygames|coolmathgames"),
    ("video", r"\byoutube\b|\btwitch\b|netflix|disney\+|prime video|\btiktok\b"),
    ("social", r"\breddit\b|instagram|facebook|\bsnapchat\b|\btwitter\b"),
]
APP_RULES = [
    ("games", r"^(steam|steamwebhelper|epicgameslauncher|robloxplayerbeta|robloxstudiobeta|minecraft|"
             r"minecraftlauncher|valorant.*|leagueclient|fortniteclient.*|genshinimpact|battle\.net|"
             r"riotclientservices)$"),
    ("onshape", r"^onshape"),
    ("laser", r"^(vls.*|laserwork.*|universallaser.*)$"),
    ("cad", r"^(freecad|fusion360|solidworks|prusa-slicer.*|orca-slicer|orcaslicer|blender|kicad|pcbnew|"
            r"eeschema|bambu-studio|cura|sldworks)$"),
    ("coding", r"^(code|code - insiders|cursor|pycharm64|idea64|studio64|webstorm64|clion64|rider64|"
               r"notepad\+\+|windowsterminal|wt|powershell|pwsh|cmd|gitkraken|githubdesktop|sublime_text|"
               r"python|pythonw|node|git-bash|mintty)$"),
    ("notes", r"^(obsidian|notion|onenote)$"),
    ("assistant", r"^claude$"),
    ("video", r"^(vlc|mpv|netflix|disneyplus|primevideo)$"),
]
# Fixed hosts kept as the site without changing the project (a homework doc stays "school"): the recipe miner tells
# Classroom, Docs and Calendar apart by them. Only the host, never a path or a document id.
KEEP_HOSTS = r"^(docs|drive|calendar)\.google\.com$"
_KEEP_RE = re.compile(KEEP_HOSTS)
_HOST_RE = [(p, re.compile(rx)) for p, rx in HOST_RULES]
_TITLE_RE = [(p, re.compile(rx)) for p, rx in TITLE_RULES]
_APP_RE = [(p, re.compile(rx)) for p, rx in APP_RULES]


class AwDown(Exception):
    """ActivityWatch did not answer (not running, not installed, wrong port, slow)."""


# ---- reading ActivityWatch ------------------------------------------------------------------------------------

def _get(path, params=None, timeout=TIMEOUT_S):
    """GET <URL><path>?params -> parsed JSON. Raises AwDown on any failure. Tests replace this function."""
    url = URL + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise AwDown(type(e).__name__) from e


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def enabled(settings=None):
    s = settings or Settings.load()
    return bool(getattr(s, "activitywatch_enabled", True))


def buckets(get=None):
    """{bucket id: info}. Raises AwDown."""
    data = (get or _get)("/api/0/buckets/")
    return data if isinstance(data, dict) else {}


def pick(bks):
    """The buckets that matter: {'window': id|None, 'afk': id|None, 'web': [ids]} (newest of each kind)."""
    def newest(kind):
        ids = [i for i, b in bks.items() if isinstance(b, dict) and b.get("type") == kind]
        return sorted(ids, key=lambda i: str(bks[i].get("last_updated") or ""), reverse=True)
    w, a = newest("currentwindow"), newest("afkstatus")
    return {"window": w[0] if w else None, "afk": a[0] if a else None, "web": newest("web.tab.current")}


def events(bucket, start=None, end=None, limit=MAX_EVENTS, get=None):
    """Raw events (newest first) of one bucket. Raw titles and URLs: callers keep them in memory only."""
    params = {"limit": int(limit)}
    if start is not None:
        params["start"] = _iso(start)
    if end is not None:
        params["end"] = _iso(end)
    data = (get or _get)(f"/api/0/buckets/{urllib.parse.quote(bucket, safe='')}/events", params)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _span(e):
    t = _ts(e.get("timestamp"))
    try:
        d = float(e.get("duration") or 0)
    except (TypeError, ValueError):
        d = 0.0
    return (t, t + max(d, 0.0)) if t is not None else (None, None)


# ---- deriving facts ---------------------------------------------------------------------------------------------

def app_name(raw):
    """'Chrome.exe' -> 'chrome'."""
    a = str(raw or "").strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(exe|app)$", "", a)


def host_of(url):
    """The web host of a URL (lower case, no www), or ''. The rest of the URL is never looked at again."""
    try:
        h = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def classify(app, title="", host="", labels=None):
    """(project, ws, host_kept). project is a rule's category or ''; ws is a workstream key when the title names one;
    host_kept is the host when a rule matched it, else '' (an unknown site is not remembered)."""
    from . import activity
    t = (title or "").lower()
    project, kept = "", ""
    if host:
        for p, rx in _HOST_RE:
            if rx.search(host):
                project, kept = p, host
                break
    if host and not kept and _KEEP_RE.search(host):
        kept = host
    if not project:
        for p, rx in _TITLE_RE:
            if rx.search(t):
                project = p
                break
    if not project:
        for p, rx in _APP_RE:
            if rx.search(app):
                project = p
                break
    ws = activity.match_ws(t, labels) if labels else None
    return project, ws or "", kept


def title_project(text, skip=()):
    """The first project (not in skip) whose title rule matches the text, or ''."""
    low = (text or "").lower()
    return next((p for p, rx in _TITLE_RE if p not in skip and rx.search(low)), "")


def is_leisure(project):
    return project in LEISURE


def _salt():
    con = store_.db()
    try:
        _ensure(con)
        row = con.execute("SELECT v FROM aw_state WHERE k='salt'").fetchone()
        if row:
            return row[0]
        salt = secrets.token_hex(8)
        con.execute("INSERT OR IGNORE INTO aw_state(k, v) VALUES('salt', ?)", (salt,))
        con.commit()
        return con.execute("SELECT v FROM aw_state WHERE k='salt'").fetchone()[0]
    finally:
        con.close()


def title_id(title, salt):
    return hashlib.sha1((salt + (title or "")).encode("utf-8", "replace")).hexdigest()[:10] if title else ""


def _cut(span, holes):
    """The pieces of span (a, b) left after removing the intervals in holes."""
    a, b = span
    out = []
    for h0, h1 in sorted(holes):
        if h1 <= a or h0 >= b:
            continue
        if h0 > a:
            out.append((a, h0))
        a = max(a, h1)
        if a >= b:
            break
    if a < b:
        out.append((a, b))
    return out


def derive(win_events, afk_events=None, web_events=None, labels=None, salt="", now=None):
    """Raw ActivityWatch events -> derived rows (pure; the only function that sees titles and URLs).
    Returns [{start, secs, app, project, ws, host, tid, afk}]; AFK stretches are rows with afk=1 and no app."""
    now = time.time() if now is None else now
    afk_spans = []
    out = []
    for e in afk_events or []:
        a, b = _span(e)
        if a is None or b - a < MIN_SLICE_S:
            continue
        if str((e.get("data") or {}).get("status")) == "afk":
            afk_spans.append((a, min(b, now)))
            out.append({"start": a, "secs": round(min(b, now) - a, 1), "app": "", "project": "afk", "ws": "",
                        "host": "", "tid": "", "afk": 1})
    web = []
    for e in web_events or []:
        a, b = _span(e)
        if a is not None:
            web.append((a, b, host_of((e.get("data") or {}).get("url"))))
    web.sort()
    web_starts = [w[0] for w in web]
    for e in win_events or []:
        a, b = _span(e)
        if a is None:
            continue
        data = e.get("data") or {}
        app, title = app_name(data.get("app")), str(data.get("title") or "")
        for s0, s1 in _cut((a, min(b, now)), afk_spans):
            if s1 - s0 < MIN_SLICE_S:
                continue
            host = ""
            if app in BROWSERS and web:
                i = bisect.bisect_right(web_starts, s1)
                best = max(((min(s1, w1) - max(s0, w0), h) for w0, w1, h in web[max(0, i - 40):i]),
                           default=(0, ""))
                host = best[1] if best[0] > 0 else ""
            project, ws, kept = classify(app, title, host, labels)
            out.append({"start": s0, "secs": round(s1 - s0, 1), "app": app, "project": project, "ws": ws,
                        "host": kept, "tid": title_id(title, salt), "afk": 0})
    return out


# ---- the store ---------------------------------------------------------------------------------------------------

def _ensure(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS aw_slices (start REAL, secs REAL, app TEXT, project TEXT, ws TEXT, host TEXT,
            tid TEXT, afk INTEGER, PRIMARY KEY (start, app, tid, afk));
        CREATE INDEX IF NOT EXISTS aw_slices_start ON aw_slices(start);
        CREATE TABLE IF NOT EXISTS aw_state (k TEXT PRIMARY KEY, v TEXT);""")


def _state(con, k, default=None):
    row = con.execute("SELECT v FROM aw_state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def _set_state(con, k, v):
    con.execute("INSERT INTO aw_state(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def save(rows, now=None):
    now = store_.now_or(now)
    con = store_.db()
    try:
        _ensure(con)
        for r in rows:
            con.execute("INSERT OR REPLACE INTO aw_slices(start, secs, app, project, ws, host, tid, afk) "
                        "VALUES(?,?,?,?,?,?,?,?)", (r["start"], r["secs"], r["app"], r["project"], r["ws"],
                                                    r["host"], r["tid"], r["afk"]))
        con.execute("DELETE FROM aw_slices WHERE start < ?", (now - KEEP_DAYS * 86400,))
        con.commit()
    finally:
        con.close()


def slices_since(since, until=None):
    """Stored derived rows with start >= since, oldest first, as dicts."""
    con = store_.db()
    try:
        _ensure(con)
        q, a = "SELECT * FROM aw_slices WHERE start >= ?", [since]
        if until is not None:
            q, a = q + " AND start < ?", a + [until]
        return [dict(r) for r in con.execute(q + " ORDER BY start", a)]
    finally:
        con.close()


def _labels(settings):
    """{workstream key: {names}} for title matching: repositories and their project note names (no disk scan beyond
    the watch roots' own listing the activity model already does)."""
    from . import activity
    try:
        names = activity.Resolver(settings).names
    except Exception:  # noqa: BLE001 - no repositories known: categories still work
        return {}
    return {k: {k, k.replace("-", " "), k.replace("_", " "), str(v).lower()} for k, v in names.items()}


def poll(settings=None, now=None, get=None, labels=None):
    """Read the events since the last pass, derive, store. Returns a report with counts only."""
    s = settings or Settings.load()
    if not enabled(s):
        return {"skipped": "activitywatch_enabled is off"}
    now = store_.now_or(now)
    try:
        bks = pick(buckets(get))
    except AwDown:
        _remember(now, False)
        return {"skipped": "ActivityWatch is not running"}
    if not bks["window"]:
        _remember(now, True)
        return {"skipped": "ActivityWatch has no window bucket yet"}
    con = store_.db()
    try:
        _ensure(con)
        mark = float(_state(con, "watermark", 0) or 0)
    finally:
        con.close()
    start = max(mark - OVERLAP_S, now - CATCH_UP_S)
    try:
        win = events(bks["window"], start, now, get=get)
        afk = events(bks["afk"], start, now, get=get) if bks["afk"] else []
        web = [e for b in bks["web"] for e in events(b, start, now, get=get)]
    except AwDown:
        _remember(now, False)
        return {"skipped": "ActivityWatch stopped answering"}
    rows = derive(win, afk, web, _labels(s) if labels is None else labels, _salt(), now)
    save(rows, now)
    con = store_.db()
    try:
        _set_state(con, "watermark", now)
        con.commit()
    finally:
        con.close()
    _remember(now, True)
    active = [r for r in rows if not r["afk"]]
    return {"slices": len(rows), "active_min": round(sum(r["secs"] for r in active) / 60, 1),
            "afk_min": round(sum(r["secs"] for r in rows if r["afk"]) / 60, 1),
            "buckets": {"window": bool(bks["window"]), "afk": bool(bks["afk"]), "web": len(bks["web"])},
            "projects": sorted({r["project"] for r in active if r["project"]})}


def _remember(now, ok):
    """When ActivityWatch last answered (doctor reads it)."""
    con = store_.db()
    try:
        _ensure(con)
        _set_state(con, "last_answer" if ok else "last_miss", now)
        con.commit()
    finally:
        con.close()


# ---- right now (the focus check) ---------------------------------------------------------------------------------

def current(get=None, now=None):
    """The foreground window now: {app, title, host, afk}, or None when ActivityWatch cannot say. The title and host
    are the raw ones, held in memory by the caller for one decision; nothing here stores or logs them."""
    now = time.time() if now is None else now
    try:
        bks = pick(buckets(get))
        if not bks["window"]:
            return None
        got = events(bks["window"], limit=1, get=get)
        if not got:
            return None
        a, b = _span(got[0])
        if b is None or now - b > STALE_S:
            return None
        data = got[0].get("data") or {}
        app, title = app_name(data.get("app")), str(data.get("title") or "")
        afk = False
        if bks["afk"]:
            last = events(bks["afk"], limit=1, get=get)
            if last:
                _, ab = _span(last[0])
                afk = str((last[0].get("data") or {}).get("status")) == "afk" and ab is not None and now - ab < STALE_S
        host = ""
        if app in BROWSERS:
            for wb in bks["web"]:
                tabs = events(wb, limit=1, get=get)
                if tabs:
                    _, tb = _span(tabs[0])
                    if tb is not None and now - tb < STALE_S:
                        host = host_of((tabs[0].get("data") or {}).get("url"))
                        break
        return {"app": app, "title": title, "host": host, "afk": afk}
    except AwDown:
        return None


def available(settings=None, get=None):
    """True when the switch is on and ActivityWatch answers with a window bucket."""
    if not enabled(settings):
        return False
    try:
        return bool(pick(buckets(get))["window"])
    except AwDown:
        return False


# ---- core job --------------------------------------------------------------------------------------------------

def poll_job(payload=None, *, settings=None, now=None, get=None):
    """Job activitywatch.poll: quiet hours and the off switch win; otherwise one pass."""
    from . import notify
    from .core import offswitch
    s = settings or Settings.load()
    if offswitch.is_off():
        return {"skipped": "Waggle is switched off"}
    if notify.in_quiet_hours(s, now):
        return {"skipped": "quiet hours"}
    return poll(s, now=now, get=get)


def register_core(now=None):
    """Called by the core at every start (idempotent): the job kind and the 10-minute timer."""
    from .core import jobs, timers
    jobs.register(JOB, poll_job, max_attempts=1, lease_s=90.0)
    timers.add(TIMER, "interval", EVERY, JOB, catch_up="skip", origin="activitywatch", now=now)
    return {"jobs": [JOB], "timers": [TIMER]}


def doctor_check(settings=None):
    """(status, detail, fix) for waggle.doctor."""
    s = settings or Settings.load()
    if not enabled(s):
        return "ok", "ActivityWatch sensing is switched off (activitywatch_enabled)", ""
    if available(s):
        return "ok", "ActivityWatch answers on 127.0.0.1:5600", ""
    return ("warn", "ActivityWatch is not answering (Waggle works without it, but cannot see work outside Claude "
            "Code)", "start the task: schtasks /run /tn ActivityWatch  (install steps: docs/ACTIVITY.md)")


# ---- app sequences for the miner ------------------------------------------------------------------------------

SEQ_GAP_S = 600
SEQ_MIN_S = 15


def unit(row):
    """One step of an app sequence: the program, and for a browser the kind of site."""
    if row["app"] in BROWSERS:
        return f"{row['app']}:{row['project'] or 'web'}"
    return row["app"]


def sequences(rows, n=3):
    """{label: [occurrence {ts, session, ws, source}]}: n different consecutive steps, each following the last
    within SEQ_GAP_S, from work apps only (system windows are skipped, leisure breaks the chain, AFK rows are skipped so a long
    absence breaks it through the gap rule)."""
    steps = []
    for r in sorted((r for r in rows if not r["afk"]), key=lambda r: r["start"]):
        if r["app"] in NOISE_APPS or r["secs"] < SEQ_MIN_S:
            continue
        u = (unit(r), r["start"], r["start"] + r["secs"], r["project"], r["ws"])
        if is_leisure(r["project"]):
            steps.append(None)  # leisure breaks the chain
        elif steps and steps[-1] and steps[-1][0] == u[0]:
            steps[-1] = (u[0], steps[-1][1], u[2], steps[-1][3] or u[3], steps[-1][4] or u[4])
        else:
            steps.append(u)
    out = {}
    for i in range(len(steps) - n + 1):
        win = steps[i:i + n]
        if any(w is None for w in win):
            continue
        if any(win[k + 1][1] - win[k][2] > SEQ_GAP_S for k in range(n - 1)):
            continue
        label = " > ".join(w[0] for w in win)
        ts = win[0][1]
        day = dt.date.fromtimestamp(ts).isoformat()
        out.setdefault(label, []).append({
            "ts": ts, "session": f"aw:{day}:{int(ts // 7200)}", "source": "activitywatch",
            "ws": next((w[4] or (w[3] if w[3] in PROJECTS else "") for w in win if w[4] or w[3] in PROJECTS), None)})
    return out


def main(argv=None):
    """python -m waggle.aw_sensor [status]: one pass (counts only, never a title) or the buckets ActivityWatch has."""
    import sys
    argv = sys.argv[1:] if argv is None else argv
    s = Settings.load()
    if argv[:1] == ["status"]:
        try:
            bks = pick(buckets())
            print(json.dumps({"running": True, "buckets": bks}))
        except AwDown:
            print(json.dumps({"running": False}))
        return 0
    print(json.dumps(poll(s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
