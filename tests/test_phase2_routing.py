import pytest

from waggle import events, quota, router


def ask_should_not_run(*a, **k):
    raise AssertionError("Sonnet should not be called")


def test_confident_simple_stays_local(settings):
    d = router.decide("rename my notes folder", settings=settings, asker=ask_should_not_run,
                      sorter=lambda t, s: ("simple", 0.9))
    assert d["route"] == "local" and d["by"] == "local-sort"


def test_low_confidence_simple_goes_to_sonnet(settings, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ans = {"route": "local", "reason": "fine locally", "reply": "", "build_prompt": "", "title": ""}
    d = router.decide("rename my notes folder", settings=settings, asker=lambda *a, **k: {"json": ans},
                      sorter=lambda t, s: ("simple", 0.4))
    assert d["by"] == "haiku" and d["sort"] == "simple"


@pytest.mark.parametrize("cat", ["reasoning", "build", "unsure"])
def test_non_simple_goes_to_sonnet(settings, monkeypatch, cat):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ans = {"route": "sonnet", "reason": "r", "reply": "answer", "build_prompt": "", "title": ""}
    d = router.decide("design a better intake", settings=settings, asker=lambda *a, **k: {"json": ans},
                      sorter=lambda t, s: (cat, 0.95))
    assert d["route"] == "sonnet" and d["sort"] == cat


def test_build_keyword_overrides_simple_sort(settings, monkeypatch):
    monkeypatch.setattr(quota, "window_usage", lambda *a, **k: 0.0)
    ans = {"route": "opus", "reason": "big", "reply": "", "build_prompt": "P", "title": "T"}
    d = router.decide("build me an app for a nonprofit", settings=settings, asker=lambda *a, **k: {"json": ans},
                      sorter=lambda t, s: ("simple", 0.99))
    assert d["route"] == "opus"


def test_local_sort_parses_and_survives_errors(settings):
    good = lambda msgs, **k: {"category": "build", "confidence": 1.7}
    assert router.local_sort.__name__  # patched in tests; exercise the real one below
    from importlib import reload
    import waggle.router as real
    reload(real)
    assert real.local_sort("x", settings, chat=good) == ("build", 1.0)
    assert real.local_sort("x", settings, chat=lambda m, **k: {"category": "nonsense"}) == ("unsure", 0.0)

    def boom(m, **k):
        raise RuntimeError("down")
    assert real.local_sort("x", settings, chat=boom) == ("unsure", 0.0)


def test_every_decision_is_logged(settings):
    router.decide("hey", settings=settings)
    ev = events.read("route")
    assert ev and ev[-1]["route"] == "local" and "latency_s" in ev[-1]
    assert events.summary()["messages"] == 1


def test_hand_over_is_instant_and_keeps_conversation(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("waggle_plugin", Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py")
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    monkeypatch.setattr(plug.learn, "learn_async", lambda *a: None)
    monkeypatch.setattr(plug, "_refresh_hq", lambda: None)
    plug._remember_turn("s1", "User", "explain PID")
    out = plug._hand_over("s1", "explain PID", "Haiku's answer")
    assert out == {"action": "block", "message": "Haiku's answer"}  # no local-model placeholder round trip
    assert plug._recent("s1") == "User: explain PID\nWaggle: Haiku's answer"
    for i in range(20):
        plug._remember_turn("s1", "User", f"m{i}")
    assert len(plug._convo["s1"]) == plug._CONVO_TURNS and plug._recent("s1").endswith("User: m19")
