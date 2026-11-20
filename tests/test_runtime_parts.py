"""In-process tests of the core's parts: sender contract, guard handling, retention, plug-in loading,
settings."""
import sys
import time
import types

import pytest

from waggle.core import events, jobs, ledger, metrics, outbox, plugins, runtime, sender, settings, store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(sender, "_transports", {})
    monkeypatch.setattr(sender, "_noticed", set())
    monkeypatch.setattr(jobs, "_handlers", {})
    yield
    store.close_all()


class Boom(Exception):
    def __init__(self, msg, **attrs):
        super().__init__(msg)
        self.__dict__.update(attrs)


# ---- sender -------------------------------------------------------------------------------------------------

def test_send_one_marks_sent_with_the_returned_id():
    sender.register("text", lambda row: f"m-{row['id']}")
    oid = outbox.enqueue("text", "hi", now=0)
    assert sender.send_one(now=1) == "sent"
    assert outbox.get(oid)["provider_msg_id"] == f"m-{oid}"
    assert sender.send_one(now=2) is None


def test_transport_failures_follow_the_contract():
    calls = []

    def transport(row):
        calls.append(row["body"])
        if row["body"] == "later":
            raise Boom("429", retry_after=42)
        if row["body"] == "gone":
            raise Boom("blocked", permanent=True)
        if row["body"] == "silent":
            return ""
        raise RuntimeError("network down")

    sender.register("text", transport)
    later = outbox.enqueue("text", "later", priority=4, now=0)
    gone = outbox.enqueue("text", "gone", priority=3, now=0)
    silent = outbox.enqueue("text", "silent", priority=2, now=0)
    down = outbox.enqueue("text", "down", priority=1, now=0)
    assert [sender.send_one(now=10) for _ in range(4)] == ["queued", "failed", "queued", "queued"]
    assert outbox.get(later)["next_try"] == 52
    assert outbox.get(gone)["state"] == "failed"
    assert outbox.get(silent)["last_error"] == "transport returned no message id"
    assert outbox.get(down)["next_try"] == 10 + outbox.retry_delay(1)
    assert calls == ["later", "gone", "silent", "down"]


def test_no_transport_rows_stay_queued_with_one_event_each():
    sender.register("text", lambda row: "1")
    v = outbox.enqueue("voice", "v", now=0)
    c = outbox.enqueue("call", "c", now=0)
    assert sender.send_one(now=1) is None
    assert sender.notice_no_transport() == 2
    assert sender.notice_no_transport() == 0
    sender._noticed.clear()  # a restarted core must not repeat the event either
    assert sender.notice_no_transport() == 0
    assert outbox.get(v)["state"] == outbox.get(c)["state"] == "queued"
    assert sorted(e["ref"] for e in events.query("outbox.no_transport")) == sorted([str(v), str(c)])
    with pytest.raises(ValueError):
        sender.register("fax", lambda row: "1")


# ---- guard ----------------------------------------------------------------------------------------------------

def _fake_guard(monkeypatch, verify):
    module = types.ModuleType("waggle.guard")
    module.verify = verify
    monkeypatch.setitem(sys.modules, "waggle.guard", module)


def test_guard_not_installed_is_reported_not_raised(monkeypatch):
    monkeypatch.setitem(sys.modules, "waggle.guard", None)
    assert runtime.check_guard()["status"] == "not installed"
    assert store.get_meta("guard.frozen") is None


def test_guard_violation_freezes_until_a_clean_check(monkeypatch):
    _fake_guard(monkeypatch, lambda: ["policy.py hash mismatch"])
    assert runtime.check_guard(now=100)["status"] == "frozen"
    assert store.get_meta("guard.frozen") == "1"
    assert events.query("integrity_violation")[0]["data"]["violations"] == ["policy.py hash mismatch"]
    assert not ledger.ready("safety_core", now=101)
    _fake_guard(monkeypatch, lambda: [])
    assert runtime.check_guard(now=200)["status"] == "ok"
    assert store.get_meta("guard.frozen") == "0"
    assert ledger.ready("safety_core", now=201)
    assert len(events.query("guard.unfrozen")) == 1


@pytest.mark.parametrize("result, frozen", [
    (True, False), (None, False), ({"ok": True, "violations": []}, False), (False, True),
    ({"violations": ["x"]}, True), ({"ok": False}, True), (types.SimpleNamespace(violations=["y"]), True)])
def test_guard_result_shapes(monkeypatch, result, frozen):
    _fake_guard(monkeypatch, lambda: result)
    assert (runtime.check_guard()["status"] == "frozen") is frozen


def test_a_guard_that_errors_fails_closed(monkeypatch):
    def verify():
        raise OSError("hash file unreadable")
    _fake_guard(monkeypatch, verify)
    assert runtime.check_guard()["status"] == "frozen"
    assert "unreadable" in events.query("integrity_violation")[0]["data"]["violations"][0]


# ---- retention ------------------------------------------------------------------------------------------------

def test_retention_prunes_old_rows_and_counts_them():
    old = time.time() - 40 * 86400
    jid = jobs.enqueue("k", now=old)
    jobs.claim("o", now=old)
    jobs.complete(jid, "o", now=old)
    oid = outbox.enqueue("text", "x", now=old)
    outbox.claim_due(now=old)
    outbox.mark_sent(oid, "m", now=old)
    keep = outbox.enqueue("text", "y")
    events.emit("ancient", now=time.time() - 100 * 86400)
    assert runtime.retention() == {"jobs": 1, "outbox": 1, "events": 1}
    today = metrics.day_of()
    assert metrics.get(today, "retention.jobs_pruned") == 1
    assert metrics.get(today, "retention.outbox_pruned") == 1
    assert jobs.get(jid) is None and outbox.get(oid) is None and outbox.get(keep)
    assert events.query("retention.pruned")[0]["data"]["jobs"] == 1


# ---- plug-ins and settings ---------------------------------------------------------------------------------------

def test_plugin_failures_are_recorded_and_loading_continues(monkeypatch):
    good = types.ModuleType("fake_good_plugin")
    good.register_core = lambda: jobs.register("good.kind", lambda p: None)
    bad = types.ModuleType("fake_bad_plugin")
    monkeypatch.setitem(sys.modules, "fake_good_plugin", good)
    monkeypatch.setitem(sys.modules, "fake_bad_plugin", bad)
    monkeypatch.setattr(plugins, "MODULES", ["fake_bad_plugin", "fake_missing_plugin", "fake_good_plugin"])
    monkeypatch.delenv("WAGGLE_CORE_PLUGINS", raising=False)
    out = plugins.load()
    assert out["loaded"] == ["fake_good_plugin"] and set(out["failed"]) == {"fake_bad_plugin", "fake_missing_plugin"}
    assert jobs.handler("good.kind") is not None
    assert ledger.ready("plugin:fake_good_plugin") and not ledger.ready("plugin:fake_bad_plugin")
    assert len(events.query("core.plugin_failed")) == 2


def test_settings_defaults_and_overrides(monkeypatch):
    for key in ("WAGGLE_CORE_PORT", "WAGGLE_CORE_WORKERS", "WAGGLE_CORE_CADENCE"):
        monkeypatch.delenv(key, raising=False)
    assert settings.port() == settings.DEFAULT_PORT == 8896
    assert settings.workers() == 4
    assert settings.Cadence.from_env().heartbeat == 15
    monkeypatch.setenv("WAGGLE_CORE_PORT", "18000")
    monkeypatch.setenv("WAGGLE_CORE_CADENCE", "heartbeat=2,unknown=5")
    assert settings.port() == 18000 and settings.Cadence.from_env().heartbeat == 2
    assert settings.path("core.lock").parent == settings.home()


# ---- scheduled task installer --------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler is Windows only")
def test_install_script_whatif_registers_nothing_and_shows_the_plan():
    import shutil
    import subprocess
    from pathlib import Path
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps is None:
        pytest.skip("PowerShell not available")
    script = Path(__file__).resolve().parents[1] / "tools" / "install-core-task.ps1"
    out = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-WhatIf"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    for needle in ("Task:              Waggle Core\n", "Task:              Waggle Core Watchdog", "-m waggle.core run",
                   "-m waggle.core watch", "RestartCount 999, RestartInterval 1 minute, MultipleInstances IgnoreNew",
                   "interval PT5M, duration indefinite", str(script.parents[1]),
                   'What if: Performing the operation "Register scheduled task" on target "Waggle Core".'):
        assert needle in text, needle
    assert "Registered:" not in text and "Started:" not in text
