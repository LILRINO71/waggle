"""waggle/feedwatch.py: first look baselines, later new entries are reported once, skips and the off switch work."""
from waggle import feedwatch
from waggle.config import Settings


def entry(n, title="v"):
    return {"title": f"{title}{n}", "link": f"https://x/{n}", "published": "2026-11-21T00:00:00Z"}


def db():
    return {"feeds": [{"name": "A", "url": "u1", "skip": ""}, {"name": "B", "url": "u2", "skip": "canary"}], "seen": {}}


def test_first_check_only_baselines_then_new_once():
    d = db()
    r = feedwatch.check(d, fetcher=lambda u: [entry(1)], dry_run=True)
    assert r["new"] == [] and r["baselined"] == ["A", "B"]
    r = feedwatch.check(d, fetcher=lambda u: [entry(2), entry(1)], dry_run=True)
    assert [n["link"] for n in r["new"]] == ["https://x/2", "https://x/2"]
    r = feedwatch.check(d, fetcher=lambda u: [entry(2), entry(1)], dry_run=True)
    assert r["new"] == []


def test_skip_pattern_and_errors():
    d = db()
    feedwatch.check(d, fetcher=lambda u: [entry(1)], dry_run=True)

    def f(u):
        if u == "u1":
            raise RuntimeError("down")
        return [entry(3, "canary-")]
    r = feedwatch.check(d, fetcher=f, dry_run=True)
    assert r["new"] == [] and r["errors"] == {"A": "down"}


def test_off_switch():
    s = Settings()
    s.feedwatch_enabled = False
    r = feedwatch.check(db(), fetcher=lambda u: 1 / 0, settings=s)
    assert r["off"] and r["new"] == []


def test_defaults_seeded_and_removable(capsys):
    assert len(feedwatch.load()["feeds"]) == 5
    feedwatch.main(["remove", "Ollama releases"])
    assert all(f["name"] != "Ollama releases" for f in feedwatch.load()["feeds"])
    feedwatch.main(["add", "Mine", "https://m/feed"])
    assert feedwatch.load()["feeds"][-1]["name"] == "Mine"
