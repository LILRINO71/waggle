"""Inbound turns through a real core process in a temporary Waggle home: POST /turn, ordering per chat,
parallel chats, duplicates, crashes inside the handler, refusal while stopping or without a handler."""
import json
import os
import time

import pytest

from test_runtime_core import Harness, core, wait_for  # noqa: F401 - `core` is the shared fixture
from waggle.core import control, events, health, inbound, outbox, settings

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the core targets Windows")


def post(body, auth=True):
    return health.fetch("/turn", body=body, auth=auth)


def turns(h):
    return [json.loads(x) for x in h.lines("turns.log")]


def ended(h, turn_id, attempt=None):
    return [t for t in turns(h) if t["turn_id"] == turn_id and t["event"] == "end"
            and (attempt is None or t["attempt"] == attempt)]


def started(h, turn_id, attempt=None):
    return [t for t in turns(h) if t["turn_id"] == turn_id and t["event"] == "start"
            and (attempt is None or t["attempt"] == attempt)]


def test_an_accepted_turn_reaches_the_handler(core: Harness):
    core.start()
    began = time.time()
    code, out = post({"chat": "telegram:me", "address": "555", "text": "hello", "dedupe_key": "tg:1"})
    assert code == 202 and out["duplicate"] is False
    assert time.time() - began < 2
    wait_for(lambda: ended(core, out["turn_id"]), what="the handler")
    row = inbound.get(out["turn_id"])
    assert row["state"] == "done" and row["attempts"] == 1 and row["address"] == "555"
    code, snap = health.fetch("/health")
    assert snap["inbound_handler"] is True and "text" in snap["transports"]
    assert snap["queues"]["inbox_waiting"] == 0 and snap["queues"]["inbox_running"] == 0
    assert "oldest_waiting_inbox_age_s" in snap
    assert "inbound handler: yes" in core.cli("status").stdout
    assert post({"chat": "c", "text": "x"}, auth=False)[0] == 401


def test_one_chat_runs_in_order_while_another_runs_in_parallel(core):
    core.start()
    a1 = post({"chat": "A", "text": "sleep:1.5 a1"})[1]["turn_id"]
    a2 = post({"chat": "A", "text": "a2"})[1]["turn_id"]
    b1 = post({"chat": "B", "text": "sleep:1.5 b1"})[1]["turn_id"]
    for t in (a1, a2, b1):
        wait_for(lambda t=t: ended(core, t), what="all three turns")
    a1_end, a2_start = ended(core, a1)[0]["t"], started(core, a2)[0]["t"]
    b1_start, b1_end = started(core, b1)[0]["t"], ended(core, b1)[0]["t"]
    a1_start = started(core, a1)[0]["t"]
    assert a1_end <= a2_start  # same chat: strictly one after the other
    assert b1_start < a1_end and a1_start < b1_end  # different chats overlap


def test_a_duplicate_is_reported_and_runs_once(core):
    core.start()
    body = {"chat": "A", "text": "once please", "dedupe_key": "tg:42"}
    first = post(body)[1]
    second = post(body)[1]
    assert second == {"turn_id": first["turn_id"], "duplicate": True}
    wait_for(lambda: ended(core, first["turn_id"]), what="the turn")
    time.sleep(1)
    assert len(started(core, first["turn_id"])) == 1


def test_a_core_killed_inside_the_handler_runs_the_turn_again_once(core):
    p = core.start()
    t = post({"chat": "A", "text": "die-once"})[1]["turn_id"]
    wait_for(lambda: started(core, t, attempt=1), what="attempt 1")
    p.kill()
    p.wait(10)
    core.start()
    wait_for(lambda: ended(core, t, attempt=2), what="attempt 2 to finish")
    row = inbound.get(t)
    assert row["state"] == "done" and row["attempts"] == 2
    assert len(started(core, t)) == 2
    assert [e["data"]["attempt"] for e in events.query("inbox.retried") if e["ref"] == t] == [2]


def test_a_second_death_fails_the_turn_and_tells_the_user(core):
    p = core.start()
    t = post({"chat": "A", "address": "555", "text": "die-always"})[1]["turn_id"]
    for attempt in (1, 2):
        wait_for(lambda a=attempt: started(core, t, attempt=a), what=f"attempt {attempt}")
        p.kill()
        p.wait(10)
        p = core.start()
    row = wait_for(lambda: inbound.get(t)["state"] == "failed" and inbound.get(t), what="the failed turn")
    assert "attempt 2" in row["error"]
    reply = wait_for(lambda: outbox.get_by_key(f"inbox-lost:{t}"), what="the honest reply")
    assert reply["chat"] == "555" and "send it again" in reply["body"]
    wait_for(lambda: outbox.get(reply["id"])["state"] == "sent", what="the reply to be sent")
    assert len(started(core, t)) == 2
    assert [e["ref"] for e in events.query("inbox.lost")] == [t]


def test_no_handler_means_503(core):
    core.start(plugins="")
    code, out = post({"chat": "A", "text": "hi"})
    assert code == 503
    assert inbound.list() == []
    assert health.fetch("/health")[1]["inbound_handler"] is False


def test_a_stopping_core_refuses_turns_and_finishes_the_running_one(core):
    p = core.start()
    t = post({"chat": "A", "text": "sleep:3 in flight"})[1]["turn_id"]
    wait_for(lambda: started(core, t), what="the turn to start")
    settings.path("core.stop").write_text("stop", encoding="utf-8")
    wait_for(lambda: (control.heartbeat() or {}).get("stopping"), timeout=5, what="the core to start stopping")
    assert post({"chat": "A", "text": "too late"})[0] == 503
    assert p.wait(30) == 0
    assert inbound.get(t)["state"] == "done"
    assert [r["text"] for r in inbound.list()] == ["sleep:3 in flight"]


def test_a_stuck_turn_is_given_up_and_the_watchdog_frees_its_chat(core):
    p = core.start(extra_env={"WAGGLE_CORE_TURN_TIMEOUT_S": "2"})
    stuck = post({"chat": "A", "address": "555", "text": "die-always"})[1]["turn_id"]
    wait_for(lambda: started(core, stuck), what="the stuck turn to start")
    wait_for(lambda: inbound.get(stuck)["state"] == "failed", what="the turn to be given up")
    assert [e["ref"] for e in events.query("inbox.timed_out")] == [stuck]
    reply = outbox.get_by_key(f"inbox-timeout:{stuck}")
    assert reply["chat"] == "555" and "send it again" in reply["body"]
    wait_for(lambda: outbox.get(reply["id"])["state"] == "sent", what="the honest reply")
    snap = health.fetch("/health")[1]
    assert snap["healthy"] is False and any("blocked" in r for r in snap["reasons"]), snap["reasons"]
    after = post({"chat": "A", "text": "after the stuck one"})[1]["turn_id"]
    out = core.cli("watch")
    assert "restarted" in out.stdout, out.stdout + out.stderr
    p.wait(60)
    wait_for(lambda: ended(core, after), what="the next message of that chat")
    assert len(started(core, stuck)) == 1  # given up, never run again
    assert inbound.get(stuck)["state"] == "failed"
