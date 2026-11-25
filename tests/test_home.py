"""Waggle Home (waggle/home.py): the snapshot, who may post, and the actions. The core is always a fake."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from waggle import dashboard, home, think


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(dashboard, "health", lambda: {"gateway": True, "ollama": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(url, body=None, headers=None, method=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(base, path, obj, token=home.TOKEN, **extra):
    headers = {"Content-Type": "application/json", **({"X-Waggle-Token": token} if token else {}), **extra}
    code, _, body = call(base + path, json.dumps(obj).encode(), headers)
    return code, json.loads(body or b"{}")


def test_snapshot_has_every_panel_and_is_json(settings, monkeypatch):
    monkeypatch.setattr(dashboard, "health", lambda: {"gateway": False})
    think.add_taste("Tell me when a friend has a big day.", settings)
    snap = home.state()
    for key in ("health", "usage", "thinking", "told", "work", "night", "reminders", "taste", "late"):
        assert key in snap, key
    assert snap["taste"][0].startswith("Tell me when a friend") and snap["thinking"]["times"]
    json.dumps(snap, default=str)


def test_page_carries_the_token_and_the_state_is_served(srv):
    code, _, page = call(srv + "/")
    assert code == 200 and home.TOKEN in page.decode() and "__TOKEN__" not in page.decode()
    code, _, body = call(srv + "/api/home")
    assert code == 200 and "thinking" in json.loads(body)


def test_a_post_needs_the_page_token(srv, monkeypatch):
    monkeypatch.setattr(home, "say", lambda text: {"ok": True, "text": "hi " + text})
    assert post(srv, "/api/say", {"text": "x"}, token="")[0] == 403
    assert post(srv, "/api/say", {"text": "x"}, token="wrong")[0] == 403
    assert post(srv, "/api/say", {"text": "x"}) == (200, {"ok": True, "text": "hi x"})


def test_another_site_or_another_host_name_is_refused(srv, monkeypatch):
    monkeypatch.setattr(home, "say", lambda text: {"ok": True})
    assert post(srv, "/api/say", {"text": "x"}, Origin="https://evil.example")[0] == 403
    assert post(srv, "/api/say", {"text": "x"}, Host="evil.example:8897")[0] == 403
    assert call(srv + "/", headers={"Host": "evil.example"})[0] == 403
    assert post(srv, "/api/say", {"text": "x"}, Origin=srv)[0] == 200


def test_check_post_rules():
    ok = {"Host": "127.0.0.1:8897", "X-Waggle-Token": home.TOKEN}
    assert home.check_post(ok, 8897) == ""
    assert home.check_post({**ok, "Host": "localhost:8897", "Origin": "http://localhost:8897"}, 8897) == ""
    assert home.check_post({**ok, "Host": "127.0.0.1:9999"}, 8897) == "not this machine"
    assert home.check_post({**ok, "Origin": "null"}, 8897) == "another page"
    assert home.check_post({"Host": "127.0.0.1:8897"}, 8897) == "no page token"


def test_say_goes_to_the_voice_line_as_the_dashboard_client():
    seen = {}

    def core(path, body, ctype):
        seen.update(path=path, body=json.loads(body), ctype=ctype)
        return 200, {}, json.dumps({"spoken": "Four.", "text": "2 + 2 = 4", "status": "done"}).encode()
    before = time.time()
    out = home.say("  what is 2+2 ", post=core)
    assert before <= out.pop("asked_at") <= time.time()
    assert out == {"ok": True, "text": "2 + 2 = 4", "spoken": "Four.", "status": "done"}
    assert seen["path"] == "/voice?reply=json&client=dash" and seen["body"] == {"text": "what is 2+2"}
    assert home.say("   ", post=core)["ok"] is False
    down = home.say("hi", post=lambda *a: (503, {}, b'{"error": "the core is not accepting turns now"}'))
    assert down == {"ok": False, "error": "the core is not accepting turns now"}


def test_talk_passes_audio_through_and_returns_the_spoken_words():
    def core(path, body, ctype):
        assert path == "/voice?format=wav&client=dash" and ctype == "audio/webm" and body == b"abc"
        return 200, {"Content-Type": "audio/wav", "X-Waggle-Spoken": "Good%20evening%2C%20sir."}, b"RIFF"
    code, ctype, body, spoken, status, asked_at = home.talk(b"abc", "audio/webm;codecs=opus", post=core)
    assert (code, ctype, body, spoken, status) == (200, "audio/wav", b"RIFF", "Good evening, sir.", "done")
    assert asked_at > 0

    def slow(path, body, ctype):
        return 200, {"Content-Type": "audio/wav", "X-Waggle-Spoken": "Working", "X-Waggle-Status": "working"}, b"RIFF"
    assert home.talk(b"abc", "audio/webm", post=slow)[4] == "working"
    assert home.talk(b"abc", "text/plain", post=core)[0] == 400
    assert home.talk(b"x" * (home.MAX_BODY + 1), "audio/webm", post=core)[0] == 400


def test_the_dashboard_client_is_known_to_the_voice_line():
    from waggle.core import voiceline
    assert "dash" in voiceline.CLIENT_SOURCE


def test_teach_adds_a_line_to_the_taste_note(srv, settings):
    assert post(srv, "/api/taste", {"line": "No sales stuff."}) == (200, {"ok": True})
    assert "No sales stuff." in think.taste(settings)


def test_night_answer_that_matches_nothing_says_so(srv, settings):
    code, out = post(srv, "/api/night", {"text": "accept 1"})
    assert code == 200 and out["ok"] is False


def test_think_now_runs_one_pass_at_a_time(settings):
    gate, ran = threading.Event(), []

    def slow(slot=""):
        ran.append(slot)
        gate.wait(5)
        return {"ok": True}
    assert home.think_now(runner=slow) == {"ok": True}
    assert home.think_now(runner=slow)["ok"] is False
    gate.set()
    for _ in range(100):
        if not home._thinking["running"]:
            break
        threading.Event().wait(0.05)
    assert ran == ["asked"] and home._thinking["last"] == {"ok": True}


def test_unknown_post_path_and_bad_json(srv):
    assert post(srv, "/api/nope", {})[0] == 404
    code, _, _ = call(srv + "/api/say", b"{not json", {"Content-Type": "application/json", "X-Waggle-Token": home.TOKEN})
    assert code == 400


def test_the_latest_pass_shows_what_it_thought_even_when_nothing_was_sent(settings):
    res = {"json": {"thoughts": [{"topic": "trip", "action": "text", "message": "The trip has no destination yet.",
                                  "why": "one day left"},
                                 {"topic": "small", "action": "none", "message": "Not worth a message.", "why": "x"}]}}
    think.run(settings, asker=lambda p, **k: res, dry=True, sections={"Now": "x"})
    shown = home.state()["thinking"]["thoughts"]
    assert [(t["topic"], t["action"]) for t in shown] == [("trip", "considered"), ("small", "considered")]
    assert "dry run" in shown[0]["guard"]
    think.run(settings, asker=lambda p, **k: res, sections={"Now": "x"})          # a real pass: "trip" is sent
    shown = home.state()["thinking"]["thoughts"]
    assert ("small", "considered") in [(t["topic"], t["action"]) for t in shown]
    assert [t["topic"] for t in shown].count("trip") == 1


def test_late_answers_are_only_the_dashboards_own_answers_after_the_holding_line(settings):
    from waggle.core import outbox
    meta = {"kind": "reply", "platform": "telegram", "address": "4242"}
    now = time.time()
    outbox.enqueue("text", "the long answer", chat="telegram:4242", idem_key="voice:t1", origin="voice",
                   meta={**meta, "via": "dashboard", "late": True})
    outbox.enqueue("text", "a quick answer", chat="telegram:4242", idem_key="voice:t2", origin="voice",
                   meta={**meta, "via": "dashboard"})
    outbox.enqueue("text", "from the phone", chat="telegram:4242", idem_key="voice:t3", origin="voice",
                   meta={**meta, "via": "voice line", "late": True})
    outbox.enqueue("text", "a brief", chat="telegram:4242", origin="outreach", meta={"late": True, "via": "dashboard"})
    late = home.late_answers(time.time())
    assert [a["text"] for a in late] == ["the long answer"] and late[0]["time"] >= now - 1 and late[0]["id"]
    assert home.late_answers(time.time() + 7200) == []  # older than the window
    assert home.state()["late"][0]["text"] == "the long answer"


def test_the_page_shows_late_answers_and_keeps_waiting_after_the_holding_line():
    page = home.page()
    assert 'r.status==="working")waitFor(r.asked_at)' in page and "showLate(d.late)" in page
    assert 'r.headers.get("X-Waggle-Status")==="working"' in page
