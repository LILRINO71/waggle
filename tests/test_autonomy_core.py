"""The autonomy plug-in: registration is idempotent, the core loads it when listed, the work job queues a step
only when the switch, the limit and an enabled goal allow it."""
import datetime as dt

from waggle import autonomy_core, goals
from waggle.core import jobs, plugins, timers

FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()


def run_job(kind, now=None, payload=None):
    owner = jobs.new_owner("test")
    jobs.enqueue(kind, payload or {}, now=now)
    job = jobs.claim(owner, kinds=[kind], now=now)
    result = jobs.invoke(jobs.handler(kind), job)
    jobs.complete(job["id"], owner, result, now=now)
    return result


def test_register_is_idempotent_and_seeds_disabled_goals(settings):
    first = autonomy_core.register_core()
    again = autonomy_core.register_core()
    assert first["timers"] == again["timers"] == ["autonomy.activity", "autonomy.work", "autonomy.mine",
                                                  "autonomy.report"]
    assert sorted(first["seeded"]) == ["ftc-robot", "portfolio", "schoolwork", "waggle"] and again["seeded"] == []
    assert {t["id"] for t in timers.list(origin="autonomy")} == set(first["timers"])
    assert set(jobs.kinds(detached=True)) >= {"autonomy.activity", "autonomy.step", "autonomy.mine"}
    assert set(jobs.kinds(detached=False)) >= {"autonomy.work", "autonomy.report"}
    assert all(g["status"] == "disabled" for g in goals.all_goals())


def test_the_core_loads_the_plug_in_when_it_is_listed(settings, monkeypatch):
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.autonomy_core")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    out = plugins.load(record=False)
    assert out == {"loaded": ["waggle.autonomy_core"], "failed": {}}


def test_work_job_queues_a_step_only_when_allowed(settings):
    autonomy_core.register_core()
    assert run_job("autonomy.work")["why"] == "autonomy is off"
    goals.set_autonomy(True)
    assert run_job("autonomy.work")["why"] == "no enabled goal"
    goals.add("g1", "Goal", charter={"actions": ["research"], "write_roots": [], "channels": [], "money": 0,
                                     "expires": FUTURE})
    goals.enable("g1")
    out = run_job("autonomy.work")
    assert out["queued"]
    assert run_job("autonomy.work")["job"] == out["job"]  # same hour: the same queued step, not a second one
    assert [j["kind"] for j in jobs.list(state="queued")] == ["autonomy.step"]
    goals.set_max_steps(0)
    assert "0 of 0" in run_job("autonomy.work")["why"]


def test_activity_and_report_jobs_run_on_an_empty_home(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("WAGGLE_CLAUDE_PROJECTS", str(tmp_path / "none"))
    settings.watch_roots = [str(tmp_path / "work")]  # never the real repositories
    settings.save()
    autonomy_core.register_core()
    out = run_job("autonomy.activity")
    assert out["workstreams"] == 0
    assert run_job("autonomy.report") == {"lines": []}
    assert run_job("autonomy.mine")["candidates"] == 0
