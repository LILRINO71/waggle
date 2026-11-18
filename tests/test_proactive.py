import subprocess
import time

import pytest

from waggle import events, notify, proactive, projects, quota, vault
from waggle.config import Settings


@pytest.fixture
def proj(settings):
    vault.init(settings)
    projects.create("BIOBUZZ Robot", settings, aliases=["biobuzz"], next_actions=["Research transfers"])
    settings.watch_roots = []
    settings.save()
    return Settings.load()


def review(insights):
    return lambda msgs, **k: {"insights": insights}


def test_low_confidence_is_logged_not_shown(proj):
    out = proactive.run(proj, chat=review([{"title": "Maybe check bolts", "detail": "?", "tier": "important",
                                            "confidence": 0.3}]))
    assert out == []
    f = events.read("finding")
    assert f and f[-1]["delivered"] is False and f[-1]["reason"] == "low confidence"


def test_important_needs_sonnet_and_rejection_drops_it(proj, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ins = [{"title": "Transfer ramp missing", "detail": "No ramp under rollers", "tier": "important",
            "confidence": 0.9}]
    reject = lambda *a, **k: {"json": {"verified": False, "tier": "low", "message": ""}}
    assert proactive.run(proj, chat=review(ins), asker=reject) == []
    assert events.read("finding")[-1]["reason"] == "not verified"
    assert notify.unseen() == []


def test_verified_important_waits_for_next_message(proj, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ins = [{"title": "Transfer ramp missing", "detail": "x", "tier": "important", "confidence": 0.9}]
    ok = lambda *a, **k: {"json": {"verified": True, "tier": "important", "message": "Add a ramp under the rollers."}}
    assert proactive.run(proj, chat=review(ins), asker=ok) == []  # not phoned now
    news = notify.unseen()
    assert news and "Add a ramp" in news[0]["text"]
    assert notify.PHONE_LOG == []


def test_critical_goes_to_phone_now_and_dedupes(proj, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ins = [{"title": "Robot over size limit", "detail": "x", "tier": "critical", "confidence": 0.95}]
    ok = lambda *a, **k: {"json": {"verified": True, "tier": "critical", "message": "Robot is 19.1 in; limit 18."}}
    out = proactive.run(proj, chat=review(ins), asker=ok)
    assert len(out) == 1 and "19.1 in" in out[0]
    assert proactive.run(proj, chat=review(ins), asker=ok) == []  # same finding within a week: silent


def test_verification_daily_cap(proj, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    proj.max_verifications_per_day = 1
    proj.save()
    calls = []

    def ok(*a, **k):
        calls.append(1)
        return {"json": {"verified": True, "tier": "important", "message": "m"}}
    for i in range(3):
        proactive.run(Settings.load(), chat=review([{"title": f"Issue {i}", "detail": "d", "tier": "important",
                                                     "confidence": 0.9}]), asker=ok)
    assert len(calls) == 1


def test_interesting_goes_to_digest_and_is_filtered(proj, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    proactive.run(proj, chat=review([{"title": "Try belts", "detail": "d", "tier": "interesting", "confidence": 0.8},
                                     {"title": "Consider testing", "detail": "d", "tier": "interesting",
                                      "confidence": 0.8}]))
    items = proactive._state()["digest"]
    keep_belts = lambda prompt, **k: {"json": {"keep": [i["id"] for i in items if "belts" in i["text"]]}}
    d = proactive.digest(proj, asker=keep_belts)
    assert "Try belts" in d and "Consider testing" not in d and "useful" in d
    assert proactive.digest(proj, asker=keep_belts) == ""  # sent once


def test_factual_digest_items_skip_the_filter(proj):
    st = proactive._state()
    st["digest"] = [{"id": "aaa", "text": "repo: 3 uncommitted changes", "source": "git", "verified": True}]
    proactive._save(st)
    assert "uncommitted" in proactive.digest(proj, asker=lambda *a, **k: pytest.fail("no Sonnet needed"))


def test_stale_project_watch(proj):
    import datetime as dt
    p = vault.safe_path("Projects/BIOBUZZ Robot", proj)
    p.write_text(p.read_text(encoding="utf-8").replace(f"updated: {dt.date.today():%Y-%m-%d}",
                                                       "updated: 2026-01-01"), encoding="utf-8")
    found = proactive.stale_watch(proj, today=dt.date(2026, 2, 1))
    assert found and "Research transfers" in found[0]["detail"]


def test_git_watch_unpushed_and_uncommitted(tmp_path, settings):
    vault.init(settings)
    remote = tmp_path / "remote.git"
    work = tmp_path / "code" / "myrepo"
    run = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    run("init", "--bare", "-q", str(remote))
    run("clone", "-q", str(remote), str(work))
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "a", cwd=work)
    run("push", "-q", "origin", "HEAD", cwd=work)
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "b", cwd=work)
    (work / "wip.txt").write_text("x")
    settings.watch_roots = [str(tmp_path / "code")]
    found = proactive.git_watch(settings, now=time.time() + 5 * 86400)
    titles = " | ".join(f["title"] for f in found)
    assert "1 commit(s) not pushed" in titles and "uncommitted" in titles


def test_feedback_counts_in_summary(settings):
    events.log("feedback", finding="abc", useful=True)
    events.log("feedback", finding="def", useful=False)
    assert events.summary()["useful_rate"] == 0.5
