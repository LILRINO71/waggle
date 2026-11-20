import io
import json
import time

import pytest

from waggle import call
from waggle.config import Settings


@pytest.fixture
def twilio_env(tmp_path, monkeypatch):
    p = tmp_path / "twilio.env"
    p.write_text("TWILIO_ACCOUNT_SID=AC123\nTWILIO_AUTH_TOKEN=secret\nTWILIO_FROM=+15550001111\n"
                 "TWILIO_TO=+15552223333\n", encoding="utf-8")
    monkeypatch.setattr(call, "env_path", lambda: p)
    return p


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_request():
    assert call.parse_request("call me in 10 minutes about the build") == (10.0, "the build")
    assert call.parse_request("Call me") == (0.0, "")
    assert call.parse_request("can you ring me in an hour") == (60.0, "")
    assert call.parse_request("phone me in half an hour to study") == (30.0, "study")
    assert call.parse_request("what should I call my robot") is None


def test_creds_missing_and_present(twilio_env, tmp_path, monkeypatch):
    assert call.creds()["TWILIO_TO"] == "+15552223333"
    monkeypatch.setattr(call, "env_path", lambda: tmp_path / "nope.env")
    assert call.creds() is None and not call.configured()


def test_twiml_escapes_and_repeats():
    x = call.twiml("Robot <arm> & shooter")
    assert "&lt;arm&gt; &amp; shooter" in x and x.count("shooter") == 2 and "Polly.Brian" in x


def test_call_posts_and_respects_daily_cap(twilio_env):
    sent = []

    def opener(req, timeout):
        sent.append(req)
        return FakeResp(json.dumps({"sid": "CA1"}).encode())

    s = Settings(max_calls_per_day=1)
    assert call.call("hi", settings=s, opener=opener) == {"ok": True, "why": "calling", "sid": "CA1"}
    body = sent[0].data.decode()
    assert "To=%2B15552223333" in body and "Twiml=" in body and "secret" not in body
    assert sent[0].full_url.endswith("/Accounts/AC123/Calls.json")
    assert call.call("again", settings=s, opener=opener)["ok"] is False  # cap hit
    assert call.call("asked", settings=s, opener=opener, force=True)["ok"] is True


def test_call_only_queues_one_outbox_row(tmp_path, monkeypatch):
    from waggle.core import outbox
    monkeypatch.setattr(call, "env_path", lambda: tmp_path / "none.env")
    res = call.call("x", reason="asked", force=True)
    rows = outbox.list()
    assert res["ok"] and len(rows) == 1 and rows[0]["kind"] == "call" and rows[0]["origin"] == "asked"
    assert "not set up" in call.place("x")["why"]  # the raw request still says why it can't happen


def test_schedule_and_run_due():
    call.schedule("later", 10)
    call.schedule("now", 0)
    placed = call.run_due(caller=lambda m, **kw: {"ok": True, "m": m})
    assert [p["m"] for p in placed] == ["now"]
    placed = call.run_due(now=time.time() + 11 * 60, caller=lambda m, **kw: {"ok": True, "m": m})
    assert [p["m"] for p in placed] == ["later"]
    assert call.run_due(now=time.time() + 3600, caller=lambda m, **kw: {}) == []
