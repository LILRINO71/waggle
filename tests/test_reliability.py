"""Reliability: build recovery after crashes, doctor, Hermes config backup + validation, bad model output."""
import importlib.util
from pathlib import Path

from waggle import doctor, jobs, router


def test_recover_resumes_or_relaunches_dead_jobs(settings):
    a = jobs.create("A", "p", "r")
    b = jobs.create("B", "p", "r")
    c = jobs.create("C", "p", "r")
    jobs.update(a, status="running", runner_pid=111, session_id="sess-a", attempts=1, created=0)
    jobs.update(b, status="running", runner_pid=222, attempts=1, created=0)
    jobs.update(c, status="running", runner_pid=333, attempts=2, created=0)
    spawned = []
    out = dict(jobs.recover(spawn=lambda cmd, jid: spawned.append((cmd, jid)), alive=lambda pid: False))
    assert out == {a: "resumed", b: "relaunched", c: "gave up"}
    assert ("resume", a) in spawned and ("run", b) in spawned
    assert jobs.load()[c]["status"] == "interrupted"


def test_recover_leaves_live_jobs_alone(settings):
    a = jobs.create("A", "p", "r")
    jobs.update(a, status="running", runner_pid=111, created=0)
    assert jobs.recover(spawn=lambda *x: 1 / 0, alive=lambda pid: True) == []


def test_alive_on_this_process():
    import os
    assert jobs._alive(os.getpid())
    assert not jobs._alive(None)


def test_doctor_reports_and_never_raises(monkeypatch):
    monkeypatch.setattr(doctor, "check_gateway", lambda: ("fail", "down", "reload"))
    monkeypatch.setattr(doctor, "check_voice", lambda: 1 / 0)
    rows = doctor.run_all()
    by = {r["check"]: r for r in rows}
    assert by["Gateway + bridge"]["status"] == "fail" and by["Voice"]["status"] == "warn"
    text = doctor.report(rows)
    assert "FAIL" in text and "fix: reload" in text


def _load_install(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    spec = importlib.util.spec_from_file_location("waggle_install", Path(__file__).parents[1] / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_config_backup_and_validation(tmp_path, monkeypatch):
    inst = _load_install(tmp_path, monkeypatch)
    cfg = tmp_path / "hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    raw = "model:\r\n  default: waggle-4b  # café\r\n".encode("utf-8-sig")
    cfg.write_bytes(raw)
    bak = inst.backup_config()
    assert bak.read_bytes() == raw  # byte-identical: BOM, CRLF and UTF-8 preserved
    assert inst.config_ok()
    cfg.write_text("model:\n  default: x  # cafÃ© â€™\n", encoding="utf-8")  # double-encoded
    assert not inst.config_ok()
    cfg.write_text("model: [unclosed\n", encoding="utf-8")
    assert not inst.config_ok()


def test_router_survives_malformed_model_output(settings, monkeypatch):
    monkeypatch.setattr(router.quota, "can_escalate", lambda m, s: (True, ""))
    for bad in ({"json": None}, {"json": {"route": "teleport"}}, {"json": {}}, {}):
        d = router.decide("please reorganise my notes folder by topic", settings=settings, asker=lambda *a, **k: bad)
        assert d["route"] == "local"
