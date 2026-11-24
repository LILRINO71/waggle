"""The watchdog watches the Telegram gateway: a dead gateway is relaunched once a minute at most, never more than
once per 5 minutes, and never from a test or scratch home."""
import json

from waggle.core import control, store


def teardown_function():
    store.close_all()


def _pidfile(tmp_path, monkeypatch, pid):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(exist_ok=True)
    (tmp_path / "hermes" / "gateway.pid").write_text(json.dumps({"pid": pid}))


def test_a_live_gateway_is_left_alone(tmp_path, monkeypatch):
    _pidfile(tmp_path, monkeypatch, 4242)
    runs = []
    assert control.gateway_pass(1000.0, alive=lambda p: True, relaunch=lambda: runs.append(1)) is None
    assert runs == []


def test_a_dead_gateway_is_relaunched_with_a_gap(tmp_path, monkeypatch):
    _pidfile(tmp_path, monkeypatch, 4242)
    runs = []
    dead = lambda p: False  # noqa: E731
    assert control.gateway_pass(1000.0, alive=dead, relaunch=lambda: runs.append(1) or True) == "gateway relaunched"
    assert control.gateway_pass(1030.0, alive=dead, relaunch=lambda: runs.append(1)) is None  # checked a minute ago
    assert control.gateway_pass(1100.0, alive=dead, relaunch=lambda: runs.append(1)) is None  # relaunched 100 s ago
    assert control.gateway_pass(1400.0, alive=dead, relaunch=lambda: runs.append(1) or True) == "gateway relaunched"
    assert len(runs) == 2


def test_never_from_a_scratch_home(tmp_path, monkeypatch):
    _pidfile(tmp_path, monkeypatch, 4242)
    monkeypatch.setattr(control, "_start_gateway_stack", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    assert control.gateway_pass(1000.0, alive=lambda p: False) is None
