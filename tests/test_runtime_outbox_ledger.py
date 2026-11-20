import pytest

from waggle.core import events, ledger, outbox, store


@pytest.fixture(autouse=True)
def _clean():
    yield
    store.close_all()


# ---- outbox ------------------------------------------------------------------------------------------------

def test_same_idem_key_never_makes_a_second_row():
    a = outbox.enqueue("text", "brief", chat="c", idem_key="brief:2026-11-19", origin="brief", now=0)
    b = outbox.enqueue("text", "brief again", chat="c", idem_key="brief:2026-11-19", origin="brief", now=1)
    assert a == b and len(outbox.list()) == 1 and outbox.get(a)["body"] == "brief"
    c = outbox.enqueue("text", "other", chat="c", now=2)
    assert c != a
    with pytest.raises(ValueError):
        outbox.enqueue("fax", "x")


def test_claim_due_order_and_not_before():
    low = outbox.enqueue("text", "low", priority=0, now=0)
    high = outbox.enqueue("text", "high", priority=5, now=0)
    later = outbox.enqueue("text", "later", not_before=100, now=0)
    rows = outbox.claim_due(now=10)
    assert [r["id"] for r in rows] == [high, low]
    assert all(r["state"] == "sending" and r["attempts"] == 1 for r in rows)
    assert outbox.claim_due(now=10) == []
    assert [r["id"] for r in outbox.claim_due(now=100)] == [later]
    assert len(outbox.claim_due(now=0, limit=1)) == 0


def test_sent_only_with_a_provider_message_id():
    oid = outbox.enqueue("text", "hi", chat="c", now=0)
    outbox.claim_due(now=0)
    for bad in (None, "", "  "):
        with pytest.raises(ValueError):
            outbox.mark_sent(oid, bad)
    assert outbox.get(oid)["state"] == "sending"
    assert outbox.mark_sent(oid, 4711, now=5) is True
    row = outbox.get(oid)
    assert row["state"] == "sent" and row["provider_msg_id"] == "4711" and row["sent_at"] == 5
    assert outbox.mark_sent(oid, 4712) is False
    assert outbox.get(oid)["provider_msg_id"] == "4711"
    assert events.query("outbox.sent")[0]["data"]["provider_msg_id"] == "4711"


def test_failed_send_is_retried_with_capped_backoff_then_fails():
    oid = outbox.enqueue("text", "hi", now=0)
    now, delays = 0.0, []
    for _ in range(outbox.MAX_ATTEMPTS - 1):
        (row,) = outbox.claim_due(now=now)
        assert outbox.mark_failed(oid, "HTTP 502", now=now) == "queued"
        next_try = outbox.get(oid)["next_try"]
        delays.append(next_try - now)
        assert outbox.claim_due(now=next_try - 1) == []
        now = next_try
    assert delays[:3] == [15, 30, 60]
    assert max(delays) == outbox.RETRY_CAP_S
    outbox.claim_due(now=now)
    assert outbox.mark_failed(oid, "HTTP 502", now=now) == "failed"
    assert outbox.get(oid)["state"] == "failed"
    assert len(events.query("outbox.retry")) == outbox.MAX_ATTEMPTS - 1
    assert events.query("outbox.failed")[0]["data"]["attempts"] == outbox.MAX_ATTEMPTS


def test_permanent_failure_and_failure_on_a_row_not_being_sent():
    oid = outbox.enqueue("text", "hi", now=0)
    assert outbox.mark_failed(oid, "x", now=0) is None
    outbox.claim_due(now=0)
    assert outbox.mark_failed(oid, "chat not found", permanent=True, now=1) == "failed"
    assert events.query("outbox.failed")[0]["data"]["permanent"] is True


def test_forced_failure_is_retried_and_delivered_once():
    oid = outbox.enqueue("text", "hello", idem_key="t:1", now=0)
    outbox.claim_due(now=0)
    outbox.mark_failed(oid, "timeout", now=0)
    (row,) = outbox.claim_due(now=outbox.retry_delay(1))
    assert row["id"] == oid and row["attempts"] == 2
    assert outbox.mark_sent(oid, "99")
    assert outbox.claim_due(now=10_000) == []
    assert [r["state"] for r in outbox.list()] == ["sent"]


def test_stale_sending_goes_back_to_queued():
    oid = outbox.enqueue("text", "hi", now=0)
    outbox.claim_due(now=0)
    assert outbox.recover(now=outbox.SENDING_STALE_S) == 0
    assert outbox.recover(now=outbox.SENDING_STALE_S + 1) == 1
    row = outbox.get(oid)
    assert row["state"] == "queued" and row["attempts"] == 1
    assert events.query("outbox.recovered")[0]["ref"] == str(oid)
    (again,) = outbox.claim_due(now=200)
    assert again["attempts"] == 2
    # A send that actually went through before the crash is still recorded when its id arrives.
    outbox.recover(now=400)
    assert outbox.mark_sent(oid, "m1") and outbox.get(oid)["state"] == "sent"


def test_expires_is_honoured():
    old = outbox.enqueue("text", "stale", expires=50, now=0)
    fresh = outbox.enqueue("text", "fresh", expires=500, now=0)
    assert [r["id"] for r in outbox.claim_due(now=60)] == [fresh]
    assert outbox.expire(now=60) == 1
    assert outbox.get(old)["state"] == "expired"
    assert events.query("outbox.expired")[0]["ref"] == str(old)
    assert outbox.expire(now=60) == 0
    # A retry that would land after expiry expires instead of queueing.
    assert outbox.mark_failed(fresh, "timeout", now=490) == "expired"
    assert outbox.get(fresh)["state"] == "expired"


# ---- capability ledger ---------------------------------------------------------------------------------------

def test_never_verified_is_not_ready():
    assert ledger.ready("telegram.send") is False
    assert ledger.status() == []


def test_success_within_ttl_and_no_later_failure_is_the_only_ready():
    ledger.record("telegram.send", True, method="bot api sendMessage", evidence_ref="msg:1", ttl_s=3600, now=1000)
    assert ledger.ready("telegram.send", now=1000)
    assert ledger.ready("telegram.send", now=4600)
    assert not ledger.ready("telegram.send", now=4601)
    ledger.record("telegram.send", False, method="bot api sendMessage", detail="HTTP 401", now=2000)
    assert not ledger.ready("telegram.send", now=2001)
    (row,) = ledger.status(now=2001)
    assert row["ready"] is False and row["reason"] == "failed after the last success"
    assert row["last_failure"] == "HTTP 401"
    ledger.record("telegram.send", True, method="bot api sendMessage", evidence_ref="msg:2", now=3000)
    assert ledger.ready("telegram.send", now=3001)
    assert ledger.status(now=3001)[0]["ttl_s"] == 3600  # ttl kept from the first record
    assert [e["kind"] for e in events.query("capability.*")] == ["capability.verified", "capability.failed",
                                                                  "capability.verified"]


def test_failure_only_and_default_ttl():
    ledger.record("call", False, method="trial call", detail="no sip account", now=10)
    assert not ledger.ready("call", now=11)
    assert ledger.status(now=11)[0]["reason"] == "never verified"
    ledger.record("voice", True, method="tts round trip", now=0)
    assert ledger.ready("voice", now=ledger.DEFAULT_TTL_S)
    assert not ledger.ready("voice", now=ledger.DEFAULT_TTL_S + 1)
    assert not ledger.ready("voice", now=-1)


def test_method_is_required():
    with pytest.raises(ValueError):
        ledger.record("x", True, method="")
    with pytest.raises(TypeError):
        ledger.record("x", True)


def test_outbox_meta_round_trips_and_defaults_to_empty():
    from waggle.core import outbox
    a = outbox.enqueue("text", "hello", meta={"title": "Reminder", "decision_id": "d1"})
    b = outbox.enqueue("text", "plain")
    assert outbox.get(a)["meta"] == {"title": "Reminder", "decision_id": "d1"}
    assert outbox.get(b)["meta"] == {}
    assert [r["meta"] for r in outbox.claim_due()] == [{"title": "Reminder", "decision_id": "d1"}, {}]
