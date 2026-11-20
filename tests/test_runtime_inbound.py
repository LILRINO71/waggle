"""Inbound messages in-process: the inbox, the handler contract, recovery, and POST /turn."""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from waggle.core import events, health, inbound, outbox, sender, store, transports


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(inbound, "_handler", None)
    yield
    store.close_all()


def _run_all(chat):
    while (row := inbound.claim_next(chat)) is not None:
        inbound.run(row)


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---- inbox -------------------------------------------------------------------------------------------------

def test_submit_stores_one_row_and_one_event():
    out = inbound.submit("telegram:me", "hello", address="123", dedupe_key="tg:1", now=10)
    assert out["duplicate"] is False and out["turn_id"]
    row = inbound.get(out["turn_id"])
    assert row["state"] == "waiting" and row["chat"] == "telegram:me" and row["address"] == "123"
    assert row["received_ts"] == 10 and row["attempts"] == 0
    (event,) = events.query("inbox.received")
    assert event["ref"] == out["turn_id"] and event["chat"] == "telegram:me"
    with pytest.raises(ValueError):
        inbound.submit("", "x")


def test_the_same_dedupe_key_never_makes_a_second_row():
    a = inbound.submit("c", "hello", dedupe_key="tg:7")
    b = inbound.submit("c", "hello again", dedupe_key="tg:7")
    assert b == {"turn_id": a["turn_id"], "duplicate": True}
    assert len(inbound.list()) == 1 and len(events.query("inbox.received")) == 1
    assert inbound.submit("c", "no key")["duplicate"] is False
    assert inbound.submit("c", "no key")["duplicate"] is False


def test_handler_gets_the_documented_message_and_runs_in_order():
    seen = []
    inbound.register(lambda msg: seen.append(msg))
    first = inbound.submit("c", "one", address="42", kind="voice", media={"file": "a.ogg"}, origin="user", now=1)
    inbound.submit("other", "elsewhere", now=2)
    second = inbound.submit("c", "two", now=3)
    assert inbound.waiting_chats() == ["c", "other"]
    _run_all("c")
    assert [m["text"] for m in seen] == ["one", "two"]
    assert seen[0] == {"turn_id": first["turn_id"], "chat": "c", "address": "42", "text": "one", "kind": "voice",
                       "media": {"file": "a.ogg"}, "platform": "telegram", "origin": "user", "received_ts": 1,
                       "attempt": 1}
    assert inbound.get(second["turn_id"])["state"] == "done"
    assert inbound.waiting_chats() == ["other"]


def test_a_handler_exception_fails_only_that_message():
    def handler(msg):
        if msg["text"] == "bad":
            raise RuntimeError("model unavailable")
    inbound.register(handler)
    bad = inbound.submit("c", "bad")
    good = inbound.submit("c", "good")
    _run_all("c")
    row = inbound.get(bad["turn_id"])
    assert row["state"] == "failed" and "model unavailable" in row["error"]
    assert inbound.get(good["turn_id"])["state"] == "done"
    assert events.query("inbox.failed")[0]["ref"] == bad["turn_id"]


def test_a_dead_runner_gets_one_more_attempt_then_an_honest_message():
    inbound.register(lambda msg: None)
    t = inbound.submit("telegram:me", "please summarise the robot notes", address="555")["turn_id"]
    dead = _dead_pid()
    for attempt in (1, 2):
        row = inbound.claim_next("telegram:me")
        assert row["attempts"] == attempt
        store.connect().execute("UPDATE inbox SET pid=? WHERE turn_id=?", (dead, t))
        assert inbound.recover() == ({"retried": 1, "lost": 0, "cancelled": 0} if attempt == 1
                                     else {"retried": 0, "lost": 1, "cancelled": 0})
    row = inbound.get(t)
    assert row["state"] == "failed" and "attempt 2" in row["error"]
    reply = outbox.get_by_key(f"inbox-lost:{t}")
    assert reply["chat"] == "555" and reply["kind"] == "text"
    assert "please summarise the robot notes" in reply["body"] and "send it again" in reply["body"]
    assert reply["meta"] == {"turn_id": t, "chat_key": "telegram:me"}
    assert events.query("inbox.lost")[0]["data"]["outbox_id"] == reply["id"]
    assert inbound.recover() == {"retried": 0, "lost": 0, "cancelled": 0}


def test_a_live_runner_is_left_alone():
    inbound.submit("c", "x")
    inbound.claim_next("c")
    assert inbound.recover() == {"retried": 0, "lost": 0, "cancelled": 0}
    assert inbound.counts()["running"] == 1


def test_lost_text_quotes_short_and_long_messages():
    assert inbound.lost_text("") == inbound.LOST_TEXT.format(quote="")
    long = inbound.lost_text("word " * 40)
    assert '..."' in long and len(long) < 200


def test_outbox_get_by_key():
    oid = outbox.enqueue("text", "x", idem_key="k:1")
    assert outbox.get_by_key("k:1")["id"] == oid and outbox.get_by_key("k:2") is None


# ---- transports: chat keys -------------------------------------------------------------------------------------

def test_chat_keys_are_sent_to_their_address(monkeypatch):
    sent = []
    monkeypatch.setattr(sender, "_transports", {})
    monkeypatch.setattr(transports.telegram, "send_row", lambda row, api=None: sent.append(row["chat"]) or "9")
    transports.register_core()
    store.connect().execute("INSERT INTO chats(chat, platform, address) VALUES('telegram:me', 'telegram', '777')")
    store.connect().execute("INSERT INTO chats(chat, platform, address) VALUES('nowhere', 'telegram', '')")
    ok = outbox.enqueue("text", "a", chat="telegram:me")
    plain = outbox.enqueue("text", "b", chat="-1001")
    home = outbox.enqueue("text", "c", chat="")
    missing = outbox.enqueue("text", "d", chat="nowhere")
    unknown = outbox.enqueue("text", "e", chat="never-seen")
    states = [sender.send_one() for _ in range(5)]
    assert states == ["sent", "sent", "sent", "failed", "failed"]
    assert sent == ["777", "-1001", ""]
    assert outbox.get(ok)["chat"] == "telegram:me"
    assert "no Telegram address" in outbox.get(missing)["last_error"]
    assert "not in the chats table" in outbox.get(unknown)["last_error"]
    assert outbox.get(home)["state"] == outbox.get(plain)["state"] == "sent"


# ---- POST /turn on an in-process server ------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(port, body, token=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/turn", data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class FakeClock:
    """Stands in for the accept-budget clock: time moves only when a test moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(health, "_clock", c)
    monkeypatch.delenv("WAGGLE_CORE_ACCEPT_BUDGET_S", raising=False)
    return c


@pytest.fixture
def server(clock):
    port = _free_port()
    state = {"accepting": True}
    srv = health.serve(port=port, accepting_fn=lambda: state["accepting"] and inbound.handler() is not None)
    yield port, health.token(), state
    srv.shutdown()
    srv.server_close()


def _settled(turn_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = inbound.get(turn_id)["state"]
        if state != "pending":
            return state
        time.sleep(0.02)
    return "pending"


def test_turn_needs_the_token_and_a_handler(server):
    port, token, state = server
    body = {"chat": "c", "text": "hi"}
    assert _post(port, body)[0] == 401
    assert _post(port, body, token="wrong")[0] == 401
    assert _post(port, body, token=token)[0] == 503  # no handler registered
    inbound.register(lambda msg: None)
    state["accepting"] = False  # stopping
    assert _post(port, body, token=token)[0] == 503
    assert inbound.list() == []


def test_turn_accepts_validates_and_dedupes(server):
    port, token, _ = server
    inbound.register(lambda msg: None)
    body = {"chat": "telegram:me", "address": 123, "text": "hi", "kind": "text", "media": None,
            "platform": "telegram", "origin": "user", "dedupe_key": "tg:99"}
    code, out = _post(port, body, token=token)
    assert code == 202 and out["duplicate"] is False
    assert _post(port, body, token=token) == (202, {"turn_id": out["turn_id"], "duplicate": True})
    assert _settled(out["turn_id"]) == "waiting"
    assert inbound.get(out["turn_id"])["address"] == "123"
    for bad in ({"text": "no chat"}, {"chat": "c"}, {"chat": "c", "text": 5}, {"chat": True, "text": "x"}, [1]):
        assert _post(port, bad, token=token)[0] == 400, bad
    assert _post(port, None, token=token, raw=b"not json")[0] == 400
    assert len(inbound.list()) == 1


# ---- the accept budget: a late 202 must never lead to a second answer -------------------------------------------

def test_fast_accept_makes_the_turn_runnable(server, clock):
    port, token, _ = server
    inbound.register(lambda msg: None)
    code, out = _post(port, {"chat": "c", "text": "hi"}, token=token)
    assert code == 202
    assert _settled(out["turn_id"]) == "waiting"
    assert inbound.claim_next("c")["turn_id"] == out["turn_id"]


def test_budget_missed_before_the_reply_cancels_and_answers_503(server, clock, monkeypatch):
    port, token, _ = server
    inbound.register(lambda msg: None)
    monkeypatch.setattr(health, "_before_reply", lambda: setattr(clock, "t", clock.t + 1.5))
    code, _ = _post(port, {"chat": "c", "text": "slow"}, token=token)
    assert code == 503
    (row,) = inbound.list()
    assert row["state"] == "cancelled" and "budget" in row["error"]
    assert events.query("inbox.cancelled")[0]["ref"] == row["turn_id"]
    assert inbound.claim_next("c") is None


def test_a_failed_write_of_the_202_cancels(server, clock, monkeypatch):
    port, token, _ = server
    inbound.register(lambda msg: None)

    def broken(handler, data):
        raise BrokenPipeError("client went away")
    monkeypatch.setattr(health, "_write_reply", broken)
    try:
        _post(port, {"chat": "c", "text": "gone"}, token=token)
    except Exception:  # the client sees a broken response; that is the point
        pass
    (row,) = inbound.list()
    assert _settled(row["turn_id"]) == "cancelled"
    assert "writing the 202 failed" in inbound.get(row["turn_id"])["error"]
    assert inbound.claim_next("c") is None


def test_a_202_written_after_the_budget_still_cancels(server, clock, monkeypatch):
    port, token, _ = server
    inbound.register(lambda msg: None)
    real = health._write_reply

    def slow(handler, data):
        clock.t += 1.5
        real(handler, data)
    monkeypatch.setattr(health, "_write_reply", slow)
    code, out = _post(port, {"chat": "c", "text": "late"}, token=token)
    assert code == 202
    assert _settled(out["turn_id"]) == "cancelled"
    assert inbound.claim_next("c") is None


def test_submit_directly_is_runnable_at_once_and_a_dead_accepter_cancels():
    direct = inbound.submit("c", "direct")["turn_id"]
    assert inbound.get(direct)["state"] == "waiting"
    orphan = inbound.submit_pending("c", "orphan")["turn_id"]
    assert inbound.recover()["cancelled"] == 0  # its accepting process (this one) is alive
    store.connect().execute("UPDATE inbox SET pid=? WHERE turn_id=?", (_dead_pid(), orphan))
    assert inbound.recover()["cancelled"] == 1
    assert inbound.get(orphan)["state"] == "cancelled"
    assert inbound.confirm(orphan) is False and inbound.cancel(direct, "x") is False


# ---- turn timeout --------------------------------------------------------------------------------------------------

def test_give_up_fails_the_turn_and_tells_the_chat():
    t = inbound.submit("telegram:me", "plan the whole season", address="555")["turn_id"]
    inbound.claim_next("telegram:me")
    assert inbound.give_up(t, 1250.0) is True
    row = inbound.get(t)
    assert row["state"] == "failed" and "1250" in row["error"]
    reply = outbox.get_by_key(f"inbox-timeout:{t}")
    assert reply["chat"] == "555" and "21 minutes" in reply["body"] and "plan the whole season" in reply["body"]
    assert events.query("inbox.timed_out")[0]["data"]["outbox_id"] == reply["id"]
    assert inbound.give_up(t, 1300.0) is False
    inbound.finish(t)  # the stuck handler returning later changes nothing
    assert inbound.get(t)["state"] == "failed"
    assert inbound.recover()["retried"] == 0


def test_timeout_text_wording():
    assert "20 minutes" in inbound.timeout_text("x", 1200)
    assert "1 minute " in inbound.timeout_text("x", 61)
    assert "5 seconds" in inbound.timeout_text("", 5)
