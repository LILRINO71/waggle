"""Knowledge on the core: jobs and timers registered at start, handlers that finish or fail cleanly."""
import datetime as dt

import pytest

from waggle import digest, ingest, knowledge_core, learning, traces, vault
from waggle.core import jobs, plugins, timers


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture
def s(settings, tmp_path):
    settings.watch_roots = [str(tmp_path / "work")]
    settings.save()
    (tmp_path / "work").mkdir()
    vault.init(settings)
    return settings


def run_job(kind, now, payload=None):
    owner = jobs.new_owner("test")
    jobs.enqueue(kind, payload or {}, now=now)
    job = jobs.claim(owner, kinds=[kind], now=now)
    h = jobs.handler(kind)
    try:
        result = jobs.invoke(h, job)
    except Exception as e:
        jobs.fail(job["id"], owner, f"{type(e).__name__}: {e}", now=now)
    else:
        jobs.complete(job["id"], owner, result, now=now)
    return jobs.get(job["id"])


def test_the_core_loads_the_plug_in_when_it_is_listed(monkeypatch):
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.knowledge_core")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    out = plugins.load(record=False)
    assert out == {"loaded": ["waggle.knowledge_core"], "failed": {}}
    assert set(knowledge_core.KINDS) <= set(jobs.kinds(detached=True))


def test_register_core_adds_jobs_and_timers_with_their_catch_up_rules_once(s):
    report = knowledge_core.register_core(now=at(12))
    assert report["jobs"] == list(knowledge_core.KINDS)
    for kind in knowledge_core.KINDS:
        h = jobs.handler(kind)
        assert h.detached and h.timeout_s and h.max_attempts == 2
    first = {t["id"]: t for t in timers.list(origin="knowledge")}
    assert set(first) == {"knowledge.intake", "knowledge.digest", "knowledge.quiz", "knowledge.digest.am"}
    intake = first["knowledge.intake"]
    assert (intake["kind"], intake["spec"], intake["catch_up"]) == ("interval", "30m", "skip")
    for tid in ("knowledge.digest", "knowledge.quiz"):
        t = first[tid]
        assert t["kind"] == "cron" and t["catch_up"] == "latest" and t["max_late_s"] == 20 * 3600
    assert first["knowledge.digest.am"]["catch_up"] == "skip"
    knowledge_core.register_core(now=at(13))  # the next start: nothing duplicated, schedules kept
    again = {t["id"]: t for t in timers.list(origin="knowledge")}
    assert {k: v["next_run"] for k, v in again.items()} == {k: v["next_run"] for k, v in first.items()}


def test_a_nightly_timer_missed_while_the_pc_was_off_runs_once_in_the_morning(s):
    knowledge_core.register_core(now=at(12))
    fired = timers.fire_due(at(8, 0, day=20))  # off all night; on at 08:00, 11 h after 21:00
    kinds = sorted(j["kind"] for j in jobs.list(state="queued"))
    assert kinds.count("knowledge.digest") == 2 and kinds.count("knowledge.quiz") == 1  # evening (late) + 08:00
    assert kinds.count("knowledge.intake") <= 1  # skip: no pile of intakes
    late = [j for j in jobs.list(state="queued") if j["kind"] == "knowledge.quiz"][0]
    assert late["payload"]["late_s"] == int(10.5 * 3600)  # due 21:30, the PC came on at 08:00
    assert fired


# ---- the handlers ---------------------------------------------------------------------------------------

def test_intake_runs_what_is_due_without_the_digest_and_learning(s, monkeypatch):
    seen = {}

    def fake_run_due(now=None, settings=None, force=(), only=None, exclude=(), **ctx):
        seen["exclude"] = exclude
        return {"memory": {"added": 1, "seconds": 0.1}, "repos": {"error": "boom", "seconds": 0.1}}
    monkeypatch.setattr(ingest, "run_due", fake_run_due)
    knowledge_core.register_jobs()
    job = run_job("knowledge.intake", at(12))
    assert job["state"] == "done" and set(seen["exclude"]) == {"digest", "learning"}
    assert job["result"]["ran"] == ["memory", "repos"] and "repos" in job["result"]["errors"]


def test_intake_fails_cleanly_when_every_source_failed(s, monkeypatch):
    monkeypatch.setattr(ingest, "run_due", lambda **k: {"memory": {"error": "disk"}, "repos": {"error": "git"}})
    knowledge_core.register_jobs()
    job = run_job("knowledge.intake", at(12))
    assert job["state"] == "queued" and job["attempts"] == 1 and "every intake source failed" in job["error"]


def test_intake_twice_in_a_row_is_safe(s, monkeypatch):
    for k in ("memory", "repos", "github", "upcoming", "goals", "graph", "reindex"):
        monkeypatch.setitem(ingest.JOBS, k, (ingest.JOBS[k][0], ingest.JOBS[k][1], lambda s_, n, c: {"ok": 0}))
    knowledge_core.register_jobs()
    a = run_job("knowledge.intake", at(12))
    b = run_job("knowledge.intake", at(12, 5))
    assert a["state"] == b["state"] == "done"
    assert b["result"]["ran"] == []  # nothing was due the second time


def test_digest_job_distils_the_users_day_once_and_not_twice(s):
    traces.record("my hive scale reads 3.2 kg today", platform="telegram", origin="user", reply="ok")
    traces.record("a development prompt", platform="cli", reply="ok")
    calls = []

    def asker(p, **k):
        calls.append(p)
        return {"json": {"summary": "Weighed the hive.", "conversations": [], "facts": [], "outcomes": []}}
    now = dt.datetime.combine(dt.date.today(), dt.time(21, 5)).timestamp()
    knowledge_core.register_jobs()
    out = knowledge_core.digest({}, settings=s, now=now, asker=asker)
    assert len(calls) == 1 and "development prompt" not in calls[0] and out["distilled"]
    again = knowledge_core.digest({}, settings=s, now=now, asker=asker)
    assert len(calls) == 1 and again["distilled"] == []


def test_quiz_job_records_a_run_and_fails_when_retrieval_is_down(s, monkeypatch, tmp_path):
    notes = tmp_path / "memdir"
    notes.mkdir()
    (notes / "hive.md").write_text("The beekeeping club owns a ZAPPER-9 laser and one printer in the shop.\n",
                                   encoding="utf-8")
    monkeypatch.setattr(ingest, "memory_dirs", lambda settings=None: [notes])
    from conftest import bag_embed
    out = knowledge_core.quiz({}, settings=s, now=at(21, 30), embedder=bag_embed)
    assert out["quiz"]["items"] >= 1 and "error" not in out["quiz"]

    def down(texts, kind="document", settings=None):
        raise ConnectionError("no Ollama")
    from waggle import memory
    monkeypatch.setattr(memory, "relevant", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(RuntimeError, match="quiz did not run"):
        knowledge_core.quiz({}, settings=s, now=at(21, 30), embedder=down)


def test_a_handler_leaves_no_background_mark_on_the_thread_that_ran_it(s, monkeypatch):
    from waggle import budget
    monkeypatch.setattr(ingest, "run_due", lambda **k: {"memory": {"ok": 1}})
    assert not budget.is_background()
    knowledge_core.intake({})
    assert not budget.is_background()  # a chat turn on this thread afterwards is still interactive
    seen = []
    monkeypatch.setattr(ingest, "run_due", lambda **k: seen.append(budget.is_background()) or {})
    knowledge_core.intake({})
    assert seen == [True]
