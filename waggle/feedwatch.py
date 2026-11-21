"""Things worth watching: new posts and releases from a short list of feeds, no model, no account.

Reads feeds through Hermes's official `rss-feeds` skill (scripts/feed.py, standard library only; install with
`hermes skills install official/research/rss-feeds`, with HERMES_HOME set to ~/.hermes). The list lives in
~/.waggle/feeds.json, seeded once with a few defaults (the FIRST blog, the FTC SDK, and the releases of Ollama,
faster-whisper and Hermes). Remove any of them with `python -m waggle.feedwatch remove NAME`. A feed's first check
only records what is already there.

    python -m waggle.feedwatch list | add NAME URL [--skip REGEX] | remove NAME | check [--dry-run] | status

Off switch: config `feedwatch_enabled` false. Removal: delete this file, tests/test_feedwatch.py, skills/watch-feeds.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .config import Settings, data_path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULTS = [
    {"name": "FIRST blog", "url": "https://www.firstinspires.org/community-blog/rss.xml", "skip": ""},
    {"name": "FTC SDK releases", "url": "https://github.com/FIRST-Tech-Challenge/FtcRobotController/releases.atom", "skip": ""},
    {"name": "Ollama releases", "url": "https://github.com/ollama/ollama/releases.atom", "skip": r"-rc\d|pre-?release"},
    {"name": "faster-whisper releases", "url": "https://github.com/SYSTRAN/faster-whisper/releases.atom", "skip": ""},
    {"name": "Hermes releases", "url": "https://github.com/NousResearch/hermes-agent/releases.atom", "skip": r"canary"},
]
MAX_SEEN = 200  # links remembered per feed


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "feedwatch_enabled", True))


def store_path():
    return data_path("feeds.json")


def load():
    p = store_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"feeds": [dict(f) for f in DEFAULTS], "seen": {}}


def save(db):
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def feed_script():
    """The rss-feeds skill's script, or None when the skill is not installed."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    p = home / "skills" / "research" / "rss-feeds" / "scripts" / "feed.py"
    return p if p.exists() else None


def fetch(url, limit=10, timeout=60):
    """The entries of one feed as dicts (title, link, published), newest first. Raises RuntimeError with the reason."""
    script = feed_script()
    if not script:
        raise RuntimeError("the rss-feeds skill is not installed (hermes skills install official/research/rss-feeds)")
    p = subprocess.run([sys.executable, str(script), "read", url, "--limit", str(limit), "--json"],
                       capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode:
        raise RuntimeError((p.stderr.decode("utf-8", "replace") or out).strip()[:160] or f"exit {p.returncode}")
    data = json.loads(out)
    return data.get("entries", data) if isinstance(data, dict) else data


def check(db=None, fetcher=fetch, settings=None, dry_run=False):
    """{"new": [{feed, title, link, published}], "errors": {name: why}, "baselined": [names]}. Saves what it saw."""
    if not enabled(settings):
        return {"new": [], "errors": {}, "baselined": [], "off": True}
    db = db if db is not None else load()
    res = {"new": [], "errors": {}, "baselined": []}
    for f in db["feeds"]:
        try:
            entries = fetcher(f["url"])
        except Exception as e:  # noqa: BLE001
            res["errors"][f["name"]] = str(e)[:160]
            continue
        links = [e.get("link", "") for e in entries if e.get("link")]
        first = f["url"] not in db["seen"]
        seen = set(db["seen"].get(f["url"], []))
        skip = re.compile(f["skip"], re.I) if f.get("skip") else None
        if first:
            res["baselined"].append(f["name"])
        else:
            for e in entries:
                text = f"{e.get('title', '')} {e.get('link', '')}"
                if e.get("link") and e["link"] not in seen and not (skip and skip.search(text)):
                    res["new"].append({"feed": f["name"], "title": e.get("title", ""), "link": e["link"],
                                       "published": (e.get("published") or "")[:10]})
        db["seen"][f["url"]] = (links + [x for x in seen if x not in links])[:MAX_SEEN]
    if not dry_run:
        save(db)
    return res


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a.pop(0) if a else "status"
    db = load()
    if cmd in ("list", "status"):
        print(f"feedwatch {'on' if enabled() else 'OFF'}; rss-feeds skill {'installed' if feed_script() else 'MISSING'}")
        for f in db["feeds"]:
            print(f"- {f['name']}: {f['url']}" + (f"  (skips /{f['skip']}/)" if f.get("skip") else ""))
        return 0
    if cmd == "add" and len(a) >= 2:
        skip = a[a.index("--skip") + 1] if "--skip" in a else ""
        db["feeds"] = [f for f in db["feeds"] if f["name"] != a[0]] + [{"name": a[0], "url": a[1], "skip": skip}]
        save(db)
        print("added", a[0])
        return 0
    if cmd == "remove" and a:
        before = len(db["feeds"])
        db["feeds"] = [f for f in db["feeds"] if f["name"].lower() != a[0].lower()]
        save(db)
        print("removed" if len(db["feeds"]) < before else "no such feed")
        return 0
    if cmd == "check":
        res = check(db, dry_run="--dry-run" in a)
        if res.get("off"):
            print("feedwatch is off (config feedwatch_enabled)")
            return 0
        for n in res["new"]:
            print(f"NEW {n['feed']} {n['published']}: {n['title']}\n    {n['link']}")
        if res["baselined"]:
            print("first look (recorded, nothing reported):", ", ".join(res["baselined"]))
        for k, v in res["errors"].items():
            print(f"error {k}: {v}")
        print(f"{len(res['new'])} new")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
