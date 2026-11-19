import json

from waggle import desktop

WINDOWS = {"windows": [
    {"app_name": "cua-driver.exe", "pid": 1, "window_id": 1, "off_screen": False, "title": "Cua.AgentCursorOverlay", "z_index": 14},
    {"app_name": "claude.exe", "pid": 2, "window_id": 2, "off_screen": False, "title": "Claude", "z_index": 13},
    {"app_name": "Code.exe", "pid": 3, "window_id": 3, "off_screen": False, "title": "router.py - waggle", "z_index": 12},
    {"app_name": "chrome.exe", "pid": 4, "window_id": 4, "off_screen": True, "title": "hidden", "z_index": 11},
    {"app_name": "explorer.exe", "pid": 5, "window_id": 5, "off_screen": False, "title": "Program Manager", "z_index": 0}]}


def fake_dispatch(calls, capture=None, typed=None):
    def d(name, args):
        calls.append(args)
        if args["action"] == "list_windows":
            return json.dumps(WINDOWS)
        if args["action"] == "capture":
            return json.dumps(capture or {"elements": [{"role": "Text", "label": "NameError: x is not defined"},
                                                      {"role": "Text", "label": "NameError: x is not defined"}]})
        if args["action"] == "type":
            return json.dumps(typed or {"ok": True})
        return "{}"
    return d


def test_windows_filters_noise_and_orders_front_to_back():
    calls = []
    w = desktop.windows(fake_dispatch(calls))
    assert [x["app"] for x in w] == ["claude.exe", "Code.exe"]


def test_screen_context_reads_top_non_protected_window_without_vision():
    calls = []
    out = desktop.screen_context("what's this error on my screen?", fake_dispatch(calls),
                                 vision=lambda q: (_ for _ in ()).throw(AssertionError("no vision needed")))
    assert "router.py - waggle" in out and out.count("NameError") == 1 and "You CAN see" in out
    cap = [c for c in calls if c["action"] == "capture"][0]
    assert cap["pid"] == 3  # skipped the Claude window, which cua-driver refuses


def test_screen_context_uses_vision_for_looks():
    out = desktop.screen_context("what does this design look like", fake_dispatch([]), vision=lambda q: "a blue page")
    assert "a blue page" in out


def test_parse_open():
    assert desktop.parse_open("open notepad and type hello") == ("notepad", "hello")
    assert desktop.parse_open("Can you open Spotify?") == ("spotify", "")
    assert desktop.parse_open("launch calculator") == ("calculator", "")
    assert desktop.parse_open("open the door to new ideas in my essay about robotics") is None
    assert desktop.parse_open("what's on my screen") is None


def test_launch_and_type(monkeypatch):
    ran, calls = [], []
    out = desktop.open_and_type("notepad", "hello", fake_dispatch(calls), runner=ran.append, wait=0)
    assert ran == ["notepad.exe"] and out == 'Opened notepad and typed "hello".'
    assert calls[-1] == {"action": "type", "text": "hello", "app": "Notepad"}
    assert desktop.launch("definitely-not-an-app-xyz", runner=ran.append)[0] is False
