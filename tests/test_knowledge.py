"""Knowledge intake and learning: ingest, clean retrieval, conversation digests, the fact model, the daily loop.
All offline: fake embedder, fake Claude, fake gh, real git in a temp folder. Fixture content is invented."""
import datetime as dt
import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import bag_embed
from waggle import digest, events, ingest, kstore, learn, learning, memory, projects, quiz, traces, vault


# ---- helpers ---------------------------------------------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def word_embed(texts):
    """Fake embedder without hash collisions between words: a 2048-dim bag of words."""
    import hashlib
    out = []
    for t in texts:
        v = [0.0] * 2048
        for w in t.lower().replace("search_query: ", "").replace("search_document: ", "").split():
            w = w.strip(".,!?#[]*:()'" + '"' + "-")
            if len(w) > 2:
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 2048] += 1.0
        out.append(v)
    return out


def make_repo(root, name, readme="# Bee Counter\n\nCounts bees in a hive photo with a small CNN model.\n"):
    p = root / name
    p.mkdir(parents=True)
    _git(p, "init", "-q")
    (p / "README.md").write_text(readme, encoding="utf-8")
    (p / "docs").mkdir()
    (p / "docs" / "setup.md").write_text("# Setup guide\n", encoding="utf-8")
    _git(p, "add", ".")
    _git(p, "commit", "-q", "-m", "Initial commit")
    return p


def commit(p, msg):
    (p / f"f{time.time_ns()}.txt").write_text(msg, encoding="utf-8")
    _git(p, "add", ".")
    _git(p, "commit", "-q", "-m", msg)


def memdir(tmp_path):
    d = tmp_path / "claude-memory"
    d.mkdir()
    (d / "MEMORY.md").write_text("# Memory Index\n\n- [Hive tools](hive-tools.md) — shop tools\n", encoding="utf-8")
    (d / "hive-tools.md").write_text("---\nname: hive-tools\ndescription: shop tools\n---\n\nThe beekeeping club "
                                     "can only use a ZAPPER-9 laser and one printer. See [[bee-counter-notes]].\n",
                                     encoding="utf-8")
    (d / "bee-counter-notes.md").write_text("The bee-counter repo trains on 4200 labelled frames from the "
                                            "apiary camera.\n", encoding="utf-8")
    return d


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


# ---- 1a. Claude Code memory mirror -----------------------------------------------------------------

def test_mirror_is_idempotent_incremental_and_keeps_links(s, tmp_path):
    d = memdir(tmp_path)
    r1 = ingest.mirror_claude_memory(s, dirs=[d])
    assert r1["added"] == 3 and r1["updated"] == 0
    mirrored = vault.read("Knowledge/Claude Code/hive-tools", s)
    assert "[[bee-counter-notes]]" in mirrored and "ZAPPER-9" in mirrored
    assert vault.safe_path("Knowledge/Claude Code/bee-counter-notes", s).exists()  # the link target exists
    r2 = ingest.mirror_claude_memory(s, dirs=[d])
    assert r2["added"] == r2["updated"] == 0 and r2["unchanged"] == 3
    (d / "hive-tools.md").write_text("Changed: the club also has a drill press.\n", encoding="utf-8")
    r3 = ingest.mirror_claude_memory(s, dirs=[d])
    assert r3["updated"] == 1 and r3["unchanged"] == 2
    assert "drill press" in vault.read("Knowledge/Claude Code/hive-tools", s)


def test_mirror_never_overwrites_a_user_edit_silently(s, tmp_path):
    d = memdir(tmp_path)
    ingest.mirror_claude_memory(s, dirs=[d])
    vault.write("Knowledge/Claude Code/hive-tools", "my own edit\n", s)
    ingest.mirror_claude_memory(s, dirs=[d])  # source unchanged: the user's edit stays
    assert vault.read("Knowledge/Claude Code/hive-tools", s).strip() == "my own edit"
    (d / "hive-tools.md").write_text("new source text\n", encoding="utf-8")
    r = ingest.mirror_claude_memory(s, dirs=[d])
    assert r["kept_user_edit"] == 1
    archived = list((vault.root(s) / "Archive" / "Claude Code edits").glob("hive-tools-*.md"))
    assert archived and "my own edit" in archived[0].read_text(encoding="utf-8")
    (d / "bee-counter-notes.md").unlink()
    assert ingest.mirror_claude_memory(s, dirs=[d])["removed"] == 1
    assert not vault.safe_path("Knowledge/Claude Code/bee-counter-notes", s).exists()
    assert list((vault.root(s) / "Archive" / "Claude Code removed").glob("*.md"))  # moved, never deleted


def test_memory_dirs_follow_watch_roots(s, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    slug = ingest.claude_project_slug(s.watch_roots[0])
    d = tmp_path / ".claude" / "projects" / slug / "memory"
    d.mkdir(parents=True)
    assert ingest.memory_dirs(s) == [d]
    assert ingest.claude_project_slug(r"C:\Users\me\Claude") == "C--Users-me-Claude"


# ---- 1b/1c. repos and GitHub -----------------------------------------------------------------------

def test_repo_sync_creates_note_then_is_incremental(s, tmp_path):
    root = tmp_path / "work"
    repo = make_repo(root, "bee-counter")
    wt = root / "bee-counter-wt"  # a worktree: .git is a file, so it is skipped
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (root / "node_modules").mkdir()
    assert [p.name for p in ingest.discover_repos(s)] == ["bee-counter"]
    r1 = ingest.sync_repos(s)
    assert r1["created"] == 1 and not r1["errors"]
    note = vault.read("Projects/Bee Counter", s)
    assert "Counts bees in a hive photo" in note and "Setup guide" in note and "repo: [bee-counter]" in note
    assert "Waggle started following bee-counter" in note
    assert ingest.sync_repos(s)["updated"] == 0  # nothing changed -> nothing rewritten
    projects.add_task("Bee Counter", "Add hive photo augmentation", s)
    commit(repo, "Add hive photo augmentation pipeline")
    r3 = ingest.sync_repos(s)
    assert r3["new_commits"] == 1 and r3["tasks_done"] == 1
    assert projects.tasks("Bee Counter", s)[0]["done"]
    log = projects.sections("Bee Counter", s)["Log"]
    assert "1 new commit(s) in bee-counter" in log and "Ticked" in log


def test_hand_written_sections_survive_repo_and_github_updates(s, tmp_path):
    repo = make_repo(tmp_path / "work", "bee-counter")
    projects.create("Bee Counter", s, goal="My own goal words", current_focus="My focus, typed by hand",
                    decisions=["Keep the model small"])
    vault.append("Projects/Bee Counter", "## My scratch\nhand notes that must stay\n", s)
    ingest.sync_repos(s)
    commit(repo, "Tune learning rate")

    def gh(args):
        if args[0] == "pr":
            return [{"number": 7, "title": "Faster inference", "isDraft": False}]
        if args[0] == "issue":
            return []
        return [{"status": "completed", "conclusion": "success", "workflowName": "CI", "createdAt": "2026-01-02"}]
    (repo / ".git").exists()
    _git(repo, "remote", "add", "origin", "https://github.com/someone/bee-counter.git")
    assert ingest.sync_github(s, gh=gh)["ok"] == 1
    ingest.sync_repos(s)
    note = vault.read("Projects/Bee Counter", s)
    for hand in ("My own goal words", "My focus, typed by hand", "Keep the model small", "hand notes that must stay"):
        assert hand in note
    assert note.count("<!-- waggle:repo bee-counter start -->") == 1
    assert "PR #7 Faster inference" in note and "last CI: CI success" in note
    assert "Tune learning rate" in note


def test_github_failure_is_recorded_not_raised(s, tmp_path):
    repo = make_repo(tmp_path / "work", "bee-counter")
    _git(repo, "remote", "add", "origin", "git@github.com:someone/bee-counter.git")

    def gh(args):
        raise RuntimeError("HTTP 404")
    r = ingest.sync_github(s, gh=gh)
    assert r["errors"] and "not checked" in vault.read("Projects/Bee Counter", s)


def test_set_block_only_touches_its_block():
    text = "# N\n\n## Notes\nmine\n\n## Log\n- old\n"
    once = ingest.set_block(text, "x", "auto 1", heading="Auto")
    assert once.index("## Auto") < once.index("## Log") and "mine" in once
    twice = ingest.set_block(once, "x", "auto 2", heading="Auto")
    assert "auto 2" in twice and "auto 1" not in twice and twice.count("## Auto") == 1


# ---- 1d. Upcoming ---------------------------------------------------------------------------------

def test_upcoming_from_connectors_and_quiet_skip(s):
    assert "skipped" in ingest.sync_upcoming(s, connectors=[])
    today = dt.date.today()
    calls = []

    def asker(prompt, **kw):
        calls.append(kw)
        return {"json": {"events": [{"date": (today + dt.timedelta(days=2)).isoformat(), "title": "Hive inspection",
                                     "where": "club garden"},
                                    {"date": (today - dt.timedelta(days=3)).isoformat(), "title": "Old thing"}],
                         "replies": [{"date": today.isoformat(), "from": "Coach", "subject": "Field trip form",
                                      "why": "needs a signature"}]}}
    r = ingest.sync_upcoming(s, asker=asker, connectors=["Gmail", "Google_Calendar"])
    assert r["filed"] == 2 and r["rejected"] == 1
    assert calls[0]["connectors"] == ["Gmail", "Google_Calendar"] and calls[0]["purpose"] == "ingest-upcoming"
    up = vault.read("Knowledge/Upcoming", s)
    assert "Hive inspection" in up and "Field trip form" in up and "Old thing" not in up


def test_upcoming_respects_the_background_budget(s):
    s.background_daily_units = 0
    s.save()
    r = ingest.sync_upcoming(s, asker=lambda *a, **k: pytest.fail("must not call"), connectors=["Gmail"])
    assert r["skipped"].startswith("budget")


# ---- 2. retrieval ---------------------------------------------------------------------------------

def _knowledge_vault(s):
    notes = {
        "Projects/Hive Scale": "# Hive Scale\nLoad cell under the hive weighs honey flow every hour.",
        "Projects/Bee Counter": "# Bee Counter\nCNN counts bees from apiary camera frames.",
        "Me/School": "# School\nChemistry class with Mr Okafor on Tuesdays.",
        "People/People": "# People\n- **Mira**: teammate who solders the sensors",
        "Me/Likes": "# Likes\n- Jazz piano\n- Mountain biking",
        "Projects/Garden": "# Garden\nTomatoes and basil in raised beds.",
        "Ideas/Ideas": "# Ideas\n- Solar powered weather station",
        "Me/Setup": "# Setup\nDesktop with a big graphics card for local models.",
        # pollution that must never come back
        "Waggle HQ": "# Waggle HQ\nhive scale honey flow hive scale honey flow status",
        "Objectives/hive-test": "# hive scale test objective\nhive scale honey flow test",
        "Inbox/Waggle chat test": "# test\nhive scale honey flow",
        "Ideas/Digest": "# Digest\nhive scale honey flow finding",
    }
    for rel, text in notes.items():
        vault.write(rel, text, s)
    vault.journal("route=chat by=haiku - 'how is the hive scale honey flow'", s)
    memory.reindex(s, embedder=word_embed)


def test_polluting_notes_never_come_back(s):
    _knowledge_vault(s)
    bad = ("Waggle HQ.md", "Objectives/", "Inbox/", "Ideas/Digest.md")
    for q in ("hive scale honey flow", "how is the hive scale doing", "test objective status"):
        for h in memory.search(q, k=20, settings=s, embedder=word_embed):
            assert not h["path"].startswith(bad) and "route=" not in h["text"]
    block = memory.context_block("hive scale honey flow", s, embedder=word_embed, previous=False)
    assert "Projects/Hive Scale.md" in block and "route=" not in block and "Objectives/" not in block


def test_nothing_injected_when_nothing_is_relevant(s):
    _knowledge_vault(s)
    block = memory.context_block("quantum zebra xylophone", s, embedder=word_embed, previous=False)
    assert "<waggle_memory>" not in block and "<waggle_core>" in block
    hits, info = memory.relevant("honey flow load cell", s, embedder=word_embed)
    assert hits and len(hits) <= memory.MAX_INJECT and hits[0]["path"] == "Projects/Hive Scale.md"


def test_follow_up_searches_with_the_last_exchange(s):
    _knowledge_vault(s)
    q, follow = memory.query_for("and who solders them?", previous=("what sensors does the hive scale use",
                                                                    "A load cell."))
    assert follow and "hive scale" in q
    q2, follow2 = memory.query_for("tell me about the chemistry class schedule this term please",
                                   previous=("x", "y"))
    assert not follow2 and q2.startswith("tell me")
    traces.record("what does the hive scale weigh", session="t1", platform="telegram", reply="Honey flow, hourly.")
    used, follow = memory.query_for("and how often?")
    assert follow and "hive scale" in used


def test_every_injection_and_read_is_logged(s):
    _knowledge_vault(s)
    memory.context_block("hive scale honey flow", s, embedder=word_embed, previous=False, session="tg-1")
    memory.context_block("quantum zebra xylophone", s, embedder=word_embed, previous=False, session="tg-1")
    memory.read_note("Hive Scale", s, session="tg-1")
    log = memory.retrieval_log(session="tg-1")
    assert [r["mode"] for r in log] == ["inject", "inject", "read"]
    assert log[0]["paths"][0]["path"] == "Projects/Hive Scale.md" and log[0]["paths"][0]["score"] > 0
    assert log[1]["paths"] == [] and log[2]["paths"][0]["path"] == "Projects/Hive Scale.md"
    assert memory.last_retrieval()["mode"] == "read"


def test_core_block_and_related(s):
    _knowledge_vault(s)
    projects.create("Hive Scale", s, overwrite=True, current_focus="Calibrate the load cell", next_actions=["Seal box"])
    vault.write("Goals/Better honey yield", "---\ntype: goal\n---\n# Better honey yield\n\n## Goal\nMore honey\n\n"
                "Linked: [[Hive Scale]]\n", s)
    core = memory.core_block(s)
    assert "Hive Scale: Calibrate the load cell | next: Seal box" in core and len(core) < 6500
    rel = memory.related("Hive Scale", s)
    assert rel["note"] == "Projects/Hive Scale.md" and "Goals/Better honey yield.md" in rel["backlinks"]
    assert memory.read_note("nope-not-here", s)["error"]


# ---- 3. conversation digests ------------------------------------------------------------------------

def _day_turns():
    t1 = traces.record("I have a hive inspection on Saturday, and I hate long answers", session="tg", platform="telegram",
                       route="sonnet", model="sonnet", reply="Noted.")
    t2 = traces.record("find my two bee repos", session="tg", platform="telegram", route="act", model="sonnet",
                       reply="Found bee-counter and bee-counter-2027 on GitHub.",
                       steps=[{"tool": "Bash", "input": "gh repo list", "ok": True,
                               "result": "someone/bee-counter someone/bee-counter-2027"}])
    traces.record("objective step", session="objective-abc", route="act", reply="done")
    return t1, t2


def test_digest_writes_journal_block_and_keeps_hand_notes(s):
    _day_turns()
    today = dt.date.today()
    vault.write(f"Journal/{today:%Y-%m-%d}", f"# {today:%Y-%m-%d}\n\nMy own diary line.\n", s)
    r = digest.run(s, asker=lambda *a, **k: pytest.fail("no model before 20:00 for today"),
                   now=time.mktime(dt.datetime.combine(today, dt.time(12)).timetuple()))
    assert r["written"] == 1
    j = vault.read(f"Journal/{today:%Y-%m-%d}", s)
    assert "My own diary line." in j and "asked: find my two bee repos -> agent, 1 steps" in j
    assert "objective step" not in j and "route=" not in j
    assert digest.run(s, asker=None, now=time.mktime(dt.datetime.combine(today, dt.time(12)).timetuple())
                      )["written"] == 0  # idempotent


def test_distilled_day_files_verified_facts_and_outcomes_only(s):
    t1, t2 = _day_turns()
    projects.create("Bee Counter", s, aliases=["bee counter"])
    today = dt.date.today()
    sat = learn.resolve_date("saturday", today).isoformat()
    fake = {"summary": "Talked about bees.", "conversations": [{"time": "10:00", "project": "Bee Counter",
            "asked": "find repos", "found": "two repos", "decided": "", "open": "portfolio"}],
            "facts": [{"kind": "event", "text": "Hive inspection", "date": sat,
                       "quote": "I have a hive inspection on Saturday"},
                      {"kind": "preference", "text": "Wants short answers", "quote": "I hate long answers"},
                      {"kind": "like", "text": "Likes jazz", "quote": "I love jazz"}],  # never said: rejected
            "outcomes": [{"trace_id": t2, "project": "Bee Counter", "type": "found",
                          "text": "The two repos are bee-counter and bee-counter-2027"},
                         {"trace_id": t2, "project": "Bee Counter", "type": "found",
                          "text": "The repo hive-mind-9000 exists"}]}  # not in the output: rejected
    rows = digest.day_traces(today)
    r = digest.file_results(today, rows, fake, s)
    assert r == {"facts_filed": 2, "facts_rejected": 1, "outcomes_filed": 1, "outcomes_rejected": 1}
    assert "bee-counter-2027" in projects.sections("Bee Counter", s)["Log"]
    assert "Hive inspection" in vault.read("Knowledge/Upcoming", s)
    rejected = [e for e in events.read("learn") if e.get("action") == "rejected"]
    assert {e["reason"] for e in rejected} >= {"not in the user's own words"}
    assert any("specifics appear" in e["reason"] for e in rejected)
    # evening run distils today once, with the budget
    calls = []
    out = digest.run(s, asker=lambda p, **k: calls.append(k) or {"json": fake},
                     now=time.mktime(dt.datetime.combine(today, dt.time(21)).timetuple()))
    assert len(calls) == 1 and calls[0]["purpose"] == "digest" and out["distilled"]
    assert "Talked about bees." in vault.read(f"Journal/{today:%Y-%m-%d}", s)
    tomorrow = time.mktime(dt.datetime.combine(today + dt.timedelta(days=1), dt.time(8)).timetuple())
    assert "Talked about bees." in digest.yesterday_block(s, now=tomorrow)


# ---- 4. the fact model -------------------------------------------------------------------------------

def test_dated_events_get_absolute_dates_and_expire(s):
    wed = dt.date(2026, 3, 4)  # a Wednesday
    assert learn.resolve_date("meet on Saturday", wed) == dt.date(2026, 3, 7)
    assert learn.resolve_date("tomorrow", wed) == dt.date(2026, 3, 5)
    assert learn.resolve_date("due Mar 20", wed) == dt.date(2026, 3, 20)
    today = dt.date.today()
    ok, fid = learn.file_fact({"kind": "about", "key": "schedule", "text": "Has a robotics meet on Saturday"},
                              s, source="test")
    assert ok
    f = learn.facts("active", "event")[0]
    assert f["date"] == learn.resolve_date("saturday", today).isoformat() and "meet" in f["text"]
    assert "robotics meet" not in vault.read("Me/Profile.md", s)  # an event, not a profile line
    later = time.mktime(dt.datetime.combine(dt.date.fromisoformat(f["expires"]), dt.time(9)).timetuple())
    assert learn.expire(later) == 1 and not learn.facts("active", "event")
    learn.render_upcoming(s, now=later)
    assert "robotics meet" not in vault.read("Knowledge/Upcoming", s)


def test_single_value_keys_replace_and_every_rejection_is_logged(s):
    learn.file_fact({"kind": "about", "key": "grade", "text": "Sophomore"}, s, source="t")
    learn.file_fact({"kind": "about", "key": "grade", "text": "Junior (11th grade)"}, s, source="t")
    active = [f for f in learn.facts("active", "about") if f["key"] == "grade"]
    assert len(active) == 1 and "Junior" in active[0]["text"]
    assert vault.read("Me/Profile.md", s).count("**Grade:**") == 1
    bad = [{"kind": "fact", "text": "Forecast says high of 57°F"}, {"kind": "weird", "text": "whatever it is"},
           {"kind": "about", "key": "other", "text": "Deciding what to do tonight maybe"},
           {"kind": "event", "text": "Something happened", "date": "2020-01-01"}, {"kind": "like", "text": "x"}]
    for b in bad:
        assert learn.file_fact(b, s, source="t")[0] is False
    learn.file_fact({"kind": "like", "text": "Likes bee themed names"}, s, source="t")
    assert learn.file_fact({"kind": "like", "text": "Likes bee themed names"}, s, source="t") == (
        False, "duplicate of a known fact")
    rej = [e for e in events.read("learn") if e.get("action") == "rejected"]
    assert len(rej) == len(bad) + 1 and all(e.get("reason") for e in rej)


def test_learn_async_is_retired():
    assert learn.learn_async("I love jazz", "ok") is None


# ---- 5. daily learning loop and quiz -------------------------------------------------------------------

def test_quiz_is_reproducible_and_grows_with_ingest(s, tmp_path):
    d = memdir(tmp_path)
    before = quiz.run(s, embedder=bag_embed, dirs=[d])
    assert before["items"] >= 2 and before["score"] == 0.0
    ingest.mirror_claude_memory(s, dirs=[d])
    memory.reindex(s, embedder=bag_embed)
    a = quiz.run(s, embedder=bag_embed, dirs=[d])
    b = quiz.run(s, embedder=bag_embed, dirs=[d])
    assert a == b and a["known"] == 1.0 and a["score"] > before["score"]


def test_implicit_signals():
    now = time.time()
    rows = [{"id": 1, "time": now, "session": "a", "request": "show my hive scale readings", "ok": 1,
             "route": "sonnet", "model": "sonnet", "steps": [], "latency_s": 3},
            {"id": 2, "time": now + 40, "session": "a", "request": "show the hive scale readings please", "ok": 1,
             "route": "local", "model": "waggle-4b", "steps": [], "latency_s": 25},
            {"id": 3, "time": now + 90, "session": "a", "request": "email it", "ok": 0, "error": "boom",
             "route": "act", "model": "sonnet", "steps": [{"denied": 1}], "latency_s": 2, "feedback": -1}]
    sig = learning.implicit_signals(rows)
    assert [len(sig[k]) for k in ("rephrased", "local", "slow", "failed", "denied", "corrected")] == [1, 1, 1, 1, 1, 1]


def test_daily_loop_writes_note_growth_and_today_learned(s, tmp_path, monkeypatch):
    assert learning.today_learned(s) == ""
    d = memdir(tmp_path)
    monkeypatch.setattr(ingest, "memory_dirs", lambda settings=None: [d])
    ingest.mirror_claude_memory(s, dirs=[d])
    memory.reindex(s, embedder=bag_embed)
    learn.file_fact({"kind": "like", "text": "Likes mountain biking"}, s, source="t")
    out = learning.daily(s, embedder=bag_embed)
    assert out["quiz"]["items"] >= 2
    til = vault.read("Waggle/Today I learned", s)
    assert "Knowledge quiz" in til and "Likes mountain biking" in til
    g = kstore.growth(1)[-1]
    assert g["facts_filed"] == 1 and g["quiz_items"] >= 2 and g["notes"] > 0
    assert "About you: Likes mountain biking" in learning.today_learned(s)
    from waggle import hq
    assert "Growth" in hq.render_md(hq.snapshot(s), s)


# ---- scheduler, goals, cleanup ---------------------------------------------------------------------------

def test_run_due_is_idempotent(s, tmp_path, monkeypatch):
    d = memdir(tmp_path)
    make_repo(tmp_path / "work", "bee-counter")
    monkeypatch.setattr(ingest, "memory_dirs", lambda settings=None: [d])
    noon = time.mktime(dt.datetime.combine(dt.date.today(), dt.time(12)).timetuple())
    gh = lambda args: []  # noqa: E731
    r1 = ingest.run_due(now=noon, settings=s, gh=gh, connectors=[], doc_embedder=bag_embed, embedder=bag_embed)
    assert r1["memory"]["added"] == 3 and r1["repos"]["created"] == 1 and "reindex" in r1
    assert "learning" not in r1  # evening job
    r2 = ingest.run_due(now=noon + 60, settings=s, gh=gh, connectors=[], doc_embedder=bag_embed)
    assert r2 == {}
    r3 = ingest.run_due(now=noon + 3600, settings=s, gh=gh, connectors=[], doc_embedder=bag_embed)
    assert list(r3) == ["memory"] and r3["memory"]["unchanged"] == 3
    assert any(e.get("jobs") for e in events.read("ingest"))


def test_goals_get_notes_and_project_links(s):
    prof = vault.read("Me/Profile.md", s).replace("## Goals\n", "## Goals\n- Win the regional beekeeping fair\n"
                                                                 "- Learn jazz piano\n")
    vault.write("Me/Profile.md", prof, s)
    projects.create("Hive Scale", s, goal="Weigh hives for the beekeeping fair", aliases=["scale"])
    r = ingest.sync_goals(s)
    assert r["created"] == 2
    g = vault.read("Goals/Win the regional beekeeping fair", s)
    assert "[[Hive Scale]]" in g and "type: goal" in g
    assert "[[Hive Scale]]" not in vault.read("Goals/Learn jazz piano", s)
    vault.write("Goals/Learn jazz piano", vault.read("Goals/Learn jazz piano", s).replace("## Why\n",
                                                                                         "## Why\nFun.\n"), s)
    ingest.sync_goals(s)
    assert "Fun." in vault.read("Goals/Learn jazz piano", s)
    assert ingest.goals_for_project("Hive Scale", s) == ["Win the regional beekeeping fair"]


def test_cleanup_is_a_dry_run_until_applied_and_reversible(s):
    vault.write("Objectives/test-objective", "# x\n", s)
    vault.write("Inbox/Waggle chat test", "# x\n", s)
    vault.write("Inbox/Grocery list", "# my list\n", s)
    vault.write("Journal/2026-01-01", "# 2026-01-01\n\n- 08:15 route=act by=haiku - 'hi'\n", s)
    vault.write("Journal/2026-01-02", "# 2026-01-02\n\n- 08:15 route=act by=haiku - 'hi'\n- my own line\n", s)
    plan = ingest.cleanup(s)
    assert plan["dry_run"] and {p["path"] for p in plan["would_move"]} == {
        "Objectives/test-objective.md", "Inbox/Waggle chat test.md", "Journal/2026-01-01.md"}
    assert vault.safe_path("Objectives/test-objective", s).exists()
    done = ingest.cleanup(s, apply=True)
    assert len(done["moved"]) == 3 and not vault.safe_path("Objectives/test-objective", s).exists()
    assert vault.safe_path("Inbox/Grocery list", s).exists() and vault.safe_path("Journal/2026-01-02", s).exists()
    assert ingest.restore(done["id"], s) == {"restored": 3, "of": 3}
    assert vault.safe_path("Objectives/test-objective", s).exists()


def test_ingest_runs_from_the_core_intake_timer_not_hermes_cron():
    import install
    assert "waggle-ingest" in install.RETIRED_CRON  # install removes the old entry
    assert "knowledge.intake" in install.RETIRED_CRON["waggle-ingest"][1]
