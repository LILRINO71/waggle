"""Outreach on the core: transports registered with the core sender, readiness from the ledger, timing through
core timers and jobs. Every transport here is a fake: nothing reaches a real phone, ntfy or Twilio."""
import datetime as dt
import io
import json
import sys
import types
import urllib.error

import pytest

from waggle import call, notify, tick
from waggle.config import Settings
from waggle.core import outbox


def at(h, m=0, day=19):
    return dt.datetime(2026, 11, day, h, m).timestamp()


def rows():
    return sorted(outbox.list(limit=1000), key=lambda r: r["id"])


@pytest.fixture
def s(settings):
    settings.quiet_hours = [23, 7]
    settings.watch_roots = []
    settings.save()
    return Settings.load()


@pytest.fixture
def fake_sender(monkeypatch):
    """A stand-in for waggle.core.sender (being written separately): it only records registrations."""
    registered = {}
    mod = types.ModuleType("waggle.core.sender")
    mod.register = lambda kind, fn: registered.__setitem__(kind, fn)
    monkeypatch.setitem(sys.modules, "waggle.core.sender", mod)
    return registered


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(b'{"message": "no"}'))


# ---- registration ---------------------------------------------------------------------------------------

def test_register_core_registers_push_and_call_with_the_sender(s, fake_sender):
    report = tick.register_core()
    assert set(fake_sender) >= {"push", "call"} and "text" not in fake_sender
    assert fake_sender["push"] is notify.push_sender and fake_sender["call"] is call.call_sender
    assert report["senders"] == ["push", "call"]


def test_register_core_without_the_sender_module_still_starts(s, monkeypatch):
    monkeypatch.setitem(sys.modules, "waggle.core.sender", None)  # import fails
    report = tick.register_core()
    assert "sender_error" in report


# ---- push ------------------------------------------------------------------------------------------------

def _push_row(urgent=True):
    oid = notify.deliver("push", "meet in 20 min", title="Waggle", urgent=urgent, origin="initiative")["id"]
    return outbox.get(oid)


def test_push_sender_returns_the_ntfy_id(s):
    seen = []
    mid = notify.push_sender(_push_row(), post=lambda url, body: seen.append(body) or {"id": "abc123"})
    assert mid == "abc123" and seen[0]["priority"] == 5 and seen[0]["message"] == "meet in 20 min"


def test_push_sender_failures_say_whether_to_retry(s):
    def refuse(code):
        def post(url, body):
            raise http_error(code)
        return post

    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=refuse(400))
    assert e.value.permanent
    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=refuse(429))
    assert not e.value.permanent and e.value.retry_after == 60
    with pytest.raises(notify.SendError) as e:
        notify.push_sender(_push_row(), post=lambda url, body: (_ for _ in ()).throw(TimeoutError("slow")))
    assert not e.value.permanent and not hasattr(e.value, "retry_after")


# ---- call ------------------------------------------------------------------------------------------------

@pytest.fixture
def twilio(tmp_path, monkeypatch):
    env = tmp_path / "t.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)
    state = {"provisioned": True, "post": lambda req: Resp(json.dumps({"sid": "CA9"}).encode()), "posts": 0}
    monkeypatch.setattr(call, "provisioned", lambda refresh=False, opener=None, now=None: {
        "ready": state["provisioned"], "missing": "" if state["provisioned"] else call.STEP_TO})

    def opener(req, timeout):
        state["posts"] += 1
        return state["post"](req)
    state["opener"] = opener
    return state


def _call_row(now, **kw):
    oid = notify.deliver("call", "the build broke", urgent=True, origin="initiative", settings=Settings.load(),
                         now=now, **kw)["id"]
    return outbox.get(oid)


def test_call_sender_places_the_call_and_returns_the_sid(s, twilio):
    row = _call_row(at(12))
    assert call.call_sender(row, opener=twilio["opener"], now=at(12, 1)) == "CA9"
    assert twilio["posts"] == 1 and [r["kind"] for r in rows()] == ["call"]  # no fallback
    assert notify.readiness("call")["state"] != "verified"  # a placed call is not yet a call that rang


def test_call_sender_provider_failure_retries_then_falls_back(s, twilio):
    def boom(req):
        raise http_error(500)
    twilio["post"] = boom
    row = _call_row(at(12))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 1))
    assert not e.value.permanent and e.value.retry_after == 30 and len(rows()) == 1
    row["attempts"] = call.CALL_MAX_ATTEMPTS  # the last attempt also fails
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 2))
    assert e.value.permanent
    kinds = [(r["kind"], r["meta"]["urgent"]) for r in rows()[1:]]
    assert kinds == [("push", True), ("text", True)] and "Call not possible" in rows()[2]["body"]
    assert notify.readiness("call")["state"] == "failing"


def test_call_sender_refused_request_is_permanent(s, twilio):
    def refused(req):
        raise http_error(400)
    twilio["post"] = refused
    with pytest.raises(notify.SendError) as e:
        call.call_sender(_call_row(at(12)), opener=twilio["opener"], now=at(12, 1))
    assert e.value.permanent and [r["kind"] for r in rows()] == ["call", "push", "text"]


def test_call_sender_not_provisioned_never_dials(s, twilio):
    twilio["provisioned"] = False
    row = _call_row(at(12))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(12, 1))
    assert e.value.permanent and twilio["posts"] == 0
    assert [r["kind"] for r in rows()] == ["call", "push", "text"]
    with pytest.raises(notify.SendError):  # a retried sender does not double the fallback
        call.call_sender(row, opener=twilio["opener"], now=at(12, 2))
    assert len(rows()) == 3


def test_late_call_becomes_a_text_that_says_so(s, twilio):
    row = _call_row(at(7))
    with pytest.raises(notify.SendError) as e:
        call.call_sender(row, opener=twilio["opener"], now=at(9))
    assert e.value.permanent and twilio["posts"] == 0
    late = rows()[1]
    assert late["kind"] == "text" and "late: the PC was off" in late["body"] and "07:00" in late["body"]


# ---- readiness only from evidence ----------------------------------------------------------------------------

def test_readiness_is_the_ledger(s):
    from waggle.core import ledger
    assert notify.readiness("telegram_text") == {"channel": "telegram_text", "state": "not verified", "ready": False,
                                                 "line": "never delivered end to end"}
    notify.record_evidence("telegram_text", True, "message id 42 in the user's chat", ref="tg:42", now=at(10))
    assert ledger.ready("telegram_text", now=at(11)) and notify.readiness("telegram_text", now=at(11))["ready"]
    assert not notify.readiness("telegram_text", now=at(10) + 3 * 86400)["ready"]  # older than its time limit
    notify.record_evidence("telegram_text", False, "HTTP 502", now=at(12))
    assert notify.readiness("telegram_text", now=at(13))["state"] == "failing"
    assert notify.ledger_name("ntfy") == "ntfy.accepted"  # a post the server took is the weaker fact


def test_a_provisioned_twilio_account_never_makes_calls_ready(s, tmp_path, monkeypatch):
    from waggle.core import ledger
    env = tmp_path / "t.env"
    env.write_text("TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=x\nTWILIO_FROM=+15550001111\nTWILIO_TO=+15552223333\n")
    monkeypatch.setattr(call, "env_path", lambda: env)

    def opener(req, timeout):
        url = req.full_url
        if "IncomingPhoneNumbers" in url:
            return Resp(json.dumps({"incoming_phone_numbers": [{"sid": "PN1"}]}).encode())
        if "OutgoingCallerIds" in url:
            return Resp(json.dumps({"outgoing_caller_ids": [{"sid": "CI1"}]}).encode())
        if "/Calls/" in url:
            return Resp(json.dumps({"status": "completed", "duration": "14"}).encode())
        return Resp(json.dumps({"status": "active", "type": "Trial"}).encode())
    assert call.provisioned(refresh=True, opener=opener)["ready"] and call.configured()
    assert ledger.ready("call.provisioned") and not ledger.ready("call")
    assert notify.readiness("call")["state"] == "not verified"
    st = call._load_status()
    st["pending"] = [{"sid": "CA9", "time": at(12)}]
    call._save_status(st)
    assert call.check_pending(opener=opener) == [("CA9", "completed")]
    assert notify.readiness("call")["state"] == "verified" and "completed" in notify.readiness("call")["line"]
