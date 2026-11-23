"""The old Hermes cron duties as core timers: each one is registered with its old schedule, install removes the
Hermes entries (with a backup, idempotently), nothing runs twice, the OFF file stops the duties, and phone calls
that are turned off do no work and write no log."""
import datetime as dt
import json
import types
from pathlib import Path

import pytest

from waggle import call, duties_core, events, hq, kstore, learning, notify, vault
from waggle.core import jobs, offswitch, plugins, store, timers

import install


def at(h, m=0, day=22):  # 2026-11-22 is a Sunday
    return dt.datetime(2026, 11, day, h, m).timestamp()


@pytest.fixture(autouse=True)
def _close():
    yield
    store.close_all()


@pytest.fixture
def s(settings):
    vault.init(settings)
    return settings


def run_job(kind, now, payload=None):
    owner = jobs.new_owner("test")
    jobs.enqueue(kind, payload or {}, now=now)
    job = jobs.claim(owner, kinds=[kind], now=now)
    try:
        result = jobs.invoke(jobs.handler(kind), job)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job["id"], owner, f"{type(e).__name__}: {e}", now=now)
    else:
        jobs.complete(job["id"], owner, result, now=now)
    return jobs.get(job["id"])


# ---- timers ------------------------------------------------------------------------------------------------

def test_each_moved_duty_is_a_core_timer_with_its_old_schedule(s):
    report = duties_core.register_core(now=at(12))
    assert report["jobs"] == list(duties_core.KINDS)
    got = {t["id"]: (t["kind"], t["spec"], t["job_kind"]) for t in timers.list(origin="duties")}
    assert got == {  # the Hermes schedules: hq every 1m, routines every 5m, graph 0 */6, consolidate Sun 18:00, learn Sun 19:00
        "duty.hq": ("interval", "1m", "duty.hq"),
        "duty.routines": ("interval", "5m", "duty.routines"),
        "duty.graph": ("cron", "0 */6 * * *", "duty.graph"),
        "duty.consolidate": ("cron", "0 18 * * 0", "duty.consolidate"),
        "duty.approvals": ("interval", "5m", "duty.approvals"),  # Step 1: stale asks expire with one line
        "duty.learn": ("cron", "0 19 * * 0", "duty.learn"),
    }
    by = {t["id"]: t for t in timers.list(origin="duties")}
    assert by["duty.hq"]["catch_up"] == by["duty.routines"]["catch_up"] == "skip"
    assert by["duty.learn"]["catch_up"] == "latest" and by["duty.learn"]["max_late_s"] == 12 * 3600
    again_first = {k: v["next_run"] for k, v in by.items()}
    duties_core.register_core(now=at(13))  # the next start keeps the schedule and adds nothing
    again = {t["id"]: t["next_run"] for t in timers.list(origin="duties")}
    assert again == again_first and len(again) == 6


def test_the_core_loads_the_plug_in_and_it_is_in_the_production_list(monkeypatch):
    assert "waggle.duties_core" in plugins.MODULES
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS", "waggle.duties_core")
    monkeypatch.setenv("WAGGLE_CORE_PLUGINS_ONLY", "1")
    assert plugins.load(record=False) == {"loaded": ["waggle.duties_core"], "failed": {}}
    assert set(duties_core.KINDS) <= set(jobs.kinds())


def test_weekly_timers_fire_on_sunday_evening_and_hq_every_minute(s):
    duties_core.register_core(now=at(12, 0, day=21))  # Saturday noon
    timers.fire_due(at(18, 0))
    assert [j["kind"] for j in jobs.list(state="queued")].count("duty.consolidate") == 1
    assert "duty.learn" not in [j["kind"] for j in jobs.list(state="queued")]
    timers.fire_due(at(19, 0))
    assert [j["kind"] for j in jobs.list(state="queued")].count("duty.learn") == 1


def test_a_pc_that_was_off_for_hours_gets_one_dashboard_rewrite_not_a_pile(s):
    duties_core.register_core(now=at(12, 0, day=21))
    timers.fire_due(at(9, 0))  # off 21 hours: 1260 missed minutes
    kinds = [j["kind"] for j in jobs.list(state="queued")]
    assert kinds.count("duty.hq") <= 1 and kinds.count("duty.routines") <= 1


# ---- no double run -----------------------------------------------------------------------------------------

def test_every_retired_hermes_job_has_a_core_timer_behind_it(s):
    """Whatever the Hermes entries did is done by a timer the production plug-ins register: no entry is removed
    without a replacement, and no two timers share an id."""
    out = plugins.load(record=False)
    assert out["failed"] == {}, out["failed"]
    ids = [t["id"] for t in timers.list()]
    assert len(ids) == len(set(ids))
    for name, (_, replacements) in install.RETIRED_CRON.items():
        for tid in replacements:
            assert tid in ids, f"{name} is retired but {tid} is not a core timer"


def test_the_duties_do_not_repeat_what_other_timers_do(s):
    plugins.load(record=False)
    kinds = [t["job_kind"] for t in timers.list(enabled=True) if t["kind"] != "once"]
    for kind in duties_core.KINDS:
        assert kinds.count(kind) == 1  # one timer per duty
    assert "outreach.brief" in kinds and kinds.count("outreach.brief") == 1  # brief is the outreach one only


def test_the_same_scheduled_run_is_never_queued_twice(s):
    duties_core.register_core(now=at(12))
    timers.fire_due(at(13))
    timers.fire_due(at(13))  # a second scheduler pass at the same moment
    keys = [j for j in jobs.list(state="queued") if j["kind"] == "duty.graph"]
    assert len(keys) <= 1


@pytest.mark.parametrize("kind,handler", [(duties_core.ROUTINES, duties_core.routines_job),
                                          (duties_core.GRAPH, duties_core.graph_job),
                                          (duties_core.CONSOLIDATE, duties_core.consolidate_job),
                                          (duties_core.LEARN, duties_core.learn_job)])
def test_a_run_already_in_progress_is_not_run_a_second_time(s, kind, handler, monkeypatch):
    duties_core.register_jobs()
    owner = jobs.new_owner("test")
    jobs.enqueue(kind, {}, now=at(12))
    first = jobs.claim(owner, kinds=[kind], now=at(12))  # running
    jobs.enqueue(kind, {}, now=at(12, 5))
    second = jobs.claim(jobs.new_owner("test"), kinds=[kind], now=at(12, 5))
    boom = lambda *a, **k: pytest.fail("ran twice")  # noqa: E731
    monkeypatch.setattr(learning, "run", boom)
    monkeypatch.setattr("waggle.graph.refresh", boom)
    monkeypatch.setattr("waggle.learn.consolidate", boom)
    monkeypatch.setattr("waggle.routines.tick", boom)
    assert handler({}, second, settings=s) == {"skipped": "another run is in progress"}


def test_the_graph_duty_does_not_rebuild_what_the_intake_just_built(s, monkeypatch):
    seen = []
    monkeypatch.setattr("waggle.graph.refresh", lambda settings=None, vault_too=True: seen.append(vault_too) or {
        "vault": {"notes": 1} if vault_too else "built by the knowledge intake", "chat": {"turns": 0}, "stats": {}})
    kstore.put("ingest.last_run", {"graph": at(11, 0)})
    out = duties_core.graph_job({}, None, settings=s, now=at(12, 0))  # the intake did it an hour ago
    assert seen == [False] and out["vault"] == "built by the knowledge intake"
    duties_core.graph_job({}, None, settings=s, now=at(18, 0))  # seven hours ago: rebuild
    assert seen == [False, True]


# ---- the OFF file ------------------------------------------------------------------------------------------

def test_while_waggle_is_off_no_duty_does_anything(s, monkeypatch):
    boom = lambda *a, **k: pytest.fail("a duty ran while Waggle is off")  # noqa: E731
    monkeypatch.setattr(hq, "write", boom)
    monkeypatch.setattr(learning, "run", boom)
    monkeypatch.setattr("waggle.graph.refresh", boom)
    monkeypatch.setattr("waggle.learn.consolidate", boom)
    monkeypatch.setattr("waggle.routines.tick", boom)
    monkeypatch.setattr("waggle.routines.ensure_defaults", boom)
    offswitch.write(by="test")
    off = {"skipped": "Waggle is switched off"}
    assert duties_core.hq_job({}) == off
    for handler in (duties_core.routines_job, duties_core.graph_job, duties_core.consolidate_job,
                    duties_core.learn_job):
        assert handler({}, None, settings=s) == off
    offswitch.clear()
    wrote = []
    monkeypatch.setattr(hq, "write", lambda settings=None: wrote.append(1))
    assert duties_core.hq_job({}, settings=s) == {"written": True} and wrote == [1]


def test_a_damaged_off_file_counts_as_on_and_the_duties_run(s, monkeypatch):
    from waggle.core import settings as core_settings
    core_settings.path(offswitch.FILE).write_bytes(b"{cut")
    wrote = []
    monkeypatch.setattr(hq, "write", lambda settings=None: wrote.append(1))
    assert duties_core.hq_job({}, settings=s) == {"written": True} and wrote


# ---- the handlers ------------------------------------------------------------------------------------------

def test_routines_pass_recovers_supervises_then_ticks_and_survives_a_failing_step(s, monkeypatch):
    calls = []
    monkeypatch.setattr("waggle.routines.ensure_defaults", lambda: calls.append("defaults"))
    monkeypatch.setattr("waggle.jobs.recover", lambda: (_ for _ in ()).throw(RuntimeError("old jobs file")))
    monkeypatch.setattr("waggle.objectives.supervise", lambda: calls.append("supervise") or [])
    monkeypatch.setattr("waggle.routines.tick", lambda settings=None, run_fn=None, now=None: calls.append("tick") or ("r1", "ok"))
    from waggle import budget
    out = duties_core.routines_job({}, None, settings=s)
    assert calls == ["defaults", "supervise", "tick"] and out == {"ran": "r1", "status": "ok"}
    assert not budget.is_background()  # the mark did not leak into this thread


def test_weekly_learn_sends_one_text_once_even_if_it_runs_twice_and_holds_it_in_quiet_hours(s, monkeypatch):
    s.quiet_hours = [18, 7]  # Sunday 19:00 is inside these quiet hours
    s.save()
    monkeypatch.setattr(learning, "run", lambda settings=None, optimize=False: {
        "drafted": ["a", "b"], "optimize": None})
    payload = {"scheduled_ts": at(19)}
    first = duties_core.learn_job(payload, None, settings=s, now=at(19, 0))
    second = duties_core.learn_job(payload, None, settings=s, now=at(19, 1))
    assert first["sent"] and first["outbox"] == second["outbox"]  # same week: one outbox row
    row = notify.get(first["outbox"])
    assert row["origin"] == "learn" and "2 new skill(s)" in row["body"] and row["held"] is True


def test_weekly_learn_is_silent_when_nothing_was_learned(s, monkeypatch):
    monkeypatch.setattr(learning, "run", lambda settings=None, optimize=False: {"drafted": [], "optimize": None})
    out = duties_core.learn_job({"scheduled_ts": at(19)}, None, settings=s, now=at(19))
    assert out == {"drafted": 0, "sent": False, "outbox": None}


def test_consolidate_is_silent(s, monkeypatch):
    monkeypatch.setattr("waggle.learn.consolidate", lambda settings=None: "profile updated")
    before = len(notify.rows())
    assert duties_core.consolidate_job({}, None, settings=s) == {"result": "profile updated"}
    assert len(notify.rows()) == before


def test_a_failing_handler_is_a_failed_job_not_a_crash(s, monkeypatch):
    duties_core.register_jobs()
    monkeypatch.setattr(hq, "write", lambda settings=None: (_ for _ in ()).throw(OSError("vault locked")))
    job = run_job("duty.hq", at(12))
    assert job["state"] == "dead" and "vault locked" in job["error"]


# ---- install removes the Hermes entries --------------------------------------------------------------------

def _fake_hermes(home):
    """A `hermes cron remove <id>` that edits jobs.json the way Hermes does."""
    def run(*args):
        assert args[:2] == ("cron", "remove")
        f = home / "cron" / "jobs.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data["jobs"] = [j for j in data["jobs"] if j["id"] != args[2]]
        f.write_text(json.dumps(data), encoding="utf-8")
        return types.SimpleNamespace(returncode=0)
    return run


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes_live"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    names = ["waggle-proactive", "waggle-digest", "waggle-consolidate", "waggle-calls", "waggle-hq", "waggle-brief",
             "waggle-learn", "waggle-routines", "waggle-graph", "someone-elses-job"]
    jobs_ = [{"id": f"id{i}", "name": n, "enabled": True} for i, n in enumerate(names)]
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs_, "updated_at": "x"}), encoding="utf-8")
    for n, (script, _) in install.RETIRED_CRON.items():
        (home / "scripts" / script).write_text('import sys\nfrom waggle.hq import main\nmain()\n', encoding="utf-8")
    (home / "scripts" / "mine.py").write_text("print('not waggle')\n", encoding="utf-8")
    return home


def test_install_removes_the_nine_hermes_jobs_with_a_backup_and_keeps_other_jobs(hermes_home):
    original = (hermes_home / "cron" / "jobs.json").read_text(encoding="utf-8")
    out = install.retire_cron(hermes_home, run=_fake_hermes(hermes_home), now=at(9))
    assert sorted(out["removed"]) == sorted(["waggle-proactive", "waggle-digest", "waggle-consolidate", "waggle-calls",
                                             "waggle-hq", "waggle-brief", "waggle-learn", "waggle-routines",
                                             "waggle-graph"])
    assert out["failed"] == []
    left = [j["name"] for j in json.loads((hermes_home / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]]
    assert left == ["someone-elses-job"]
    backup = Path(out["backup"])
    assert backup.name.startswith("jobs.json.bak-cron2core-") and backup.read_text(encoding="utf-8") == original
    assert not (hermes_home / "scripts" / "waggle_hq.py").exists()  # the script it ran is gone
    assert (hermes_home / "scripts" / "mine.py").exists()


def test_install_cron_removal_is_idempotent_and_writes_no_second_backup(hermes_home):
    install.retire_cron(hermes_home, run=_fake_hermes(hermes_home), now=at(9))
    calls = []
    again = install.retire_cron(hermes_home, run=lambda *a: calls.append(a), now=at(9, 5))
    assert again == {"removed": [], "failed": [], "backup": None} and calls == []
    assert len(list((hermes_home / "cron").glob("jobs.json.bak-*"))) == 1


def test_a_failed_removal_is_reported_and_leaves_the_script_alone(hermes_home):
    def refuse(*args):
        return types.SimpleNamespace(returncode=1)
    out = install.retire_cron(hermes_home, run=refuse, now=at(9))
    assert out["removed"] == [] and "waggle-hq" in out["failed"] and out["backup"]
    assert (hermes_home / "scripts" / "waggle_hq.py").exists()


def test_install_without_a_hermes_cron_file_does_nothing(tmp_path):
    assert install.retire_cron(tmp_path / "nothing", run=lambda *a: pytest.fail("no call")) == {
        "removed": [], "failed": [], "backup": None}


def test_install_no_longer_creates_cron_jobs():
    assert not hasattr(install, "CRON_JOBS") and not hasattr(install, "install_cron")


def test_doctor_warns_while_a_waggle_job_is_still_in_hermes_cron(hermes_home, monkeypatch):
    from waggle import doctor
    monkeypatch.setattr(doctor, "HERMES", hermes_home)
    name, detail, fix = doctor.check_cron()[:3]
    assert name == "warn" and "waggle-hq" in detail and "install.py" in fix
    install.retire_cron(hermes_home, run=_fake_hermes(hermes_home), now=at(9))
    duties_core.register_core(now=at(12))
    plugins.load(record=False)
    assert doctor.check_cron()[0] == "ok"


# ---- phone calls turned off: no work, no log ---------------------------------------------------------------

@pytest.fixture
def twilio_not_ready(tmp_path, monkeypatch):
    env = tmp_path / "twilio.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)

    def opener(req, timeout):
        pytest.fail("a call check touched the network while calls are off")
    return opener


def _call_status_events():
    return [e for e in events.read("call_status")]


def test_calls_off_means_no_network_no_cache_write_and_no_log(s, twilio_not_ready):
    assert s.calls_enabled is False  # the default: calls are not set up
    for _ in range(5):  # the dashboard refresh asks every minute
        p = call.provisioned(opener=twilio_not_ready, now=at(12))
        assert p["ready"] is False and p["off"] is True and "turned off" in p["missing"]
    assert call.configured() is False
    assert call._load_status() == {}  # nothing cached
    assert _call_status_events() == []
    assert "turned off" in call.status_line()


def test_the_minute_dashboard_job_with_calls_off_writes_no_call_status_log(s, twilio_not_ready, monkeypatch):
    monkeypatch.setattr(call, "check_twilio", lambda *a, **k: pytest.fail("provider check ran"))
    for i in range(3):
        duties_core.hq_job({}, settings=s)
    assert _call_status_events() == []


def test_turning_calls_on_brings_the_check_back_and_off_again_silences_it(s, twilio_not_ready, monkeypatch):
    import io

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        if "IncomingPhoneNumbers" in req.full_url:
            return Resp(json.dumps({"incoming_phone_numbers": []}).encode())
        return Resp(json.dumps({"status": "active", "type": "Trial"}).encode())
    assert call.main(["enable"]) == 0 and call.enabled()
    p = call.provisioned(refresh=True, opener=opener, now=at(12))
    assert p["ready"] is False and p["missing"] == call.STEP_FROM and len(_call_status_events()) == 1
    assert call.main(["disable"]) == 0 and not call.enabled()
    call.provisioned(refresh=True, opener=twilio_not_ready, now=at(13))  # off again: quiet
    assert len(_call_status_events()) == 1


def test_a_call_that_is_wanted_while_calls_are_off_becomes_a_push_and_a_text(s, twilio_not_ready):
    row = {"id": 7, "body": "build is red", "origin": "asked", "created": at(12), "meta": {"scheduled_ts": at(12)}}
    with pytest.raises(notify.SendError, match="turned off"):
        call.call_sender(row, opener=twilio_not_ready, now=at(12, 1))
    kinds = sorted(r["kind"] for r in notify.rows())
    assert kinds == ["push", "text"]


def test_doctor_does_not_warn_about_calls_the_owner_turned_off(s, monkeypatch):
    from waggle import doctor
    assert doctor.check_phone()[0] == "ok"
    monkeypatch.setattr(call, "enabled", lambda settings=None: True)
    assert doctor.check_phone()[0] == "warn"  # on but not provisioned: say so
