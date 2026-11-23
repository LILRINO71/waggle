"""Approval preflight, background jobs on the core, and handle_inbound / register_core for the core process."""
import json

import pytest
from engine_fakes import FakeBotApi, FakeClaude, FakeClock, FakeDelivery

from waggle import chat_engine, chat_jobs, policy, traces
from waggle.chat_session import ChatSession
from waggle.config import Settings
from waggle.core import jobs, outbox, procs, store
from waggle.delivery import BotApiDelivery


@pytest.fixture(autouse=True)
def _clean():
    jobs._handlers.clear()
    yield
    jobs._handlers.clear()
    chat_engine.set_delivery_factory("telegram", None)
    store.close_all()


# ---- approvals ----------------------------------------------------------------------------------------------

def test_approve_by_code_for_this_chat_becomes_a_note():
    aid = policy.request_approval("Bash", {"command": "rm -r build"}, "deleting files", session="telegram:5")
    code = policy.get(aid)["code"]
    kind, text = chat_engine.approval_preflight("telegram:5", f"approve {code}")
    assert kind == "note" and "approved" in text and policy.get(aid)["state"] == "granted"


def test_deny_unknown_and_used_codes_answer_directly():
    aid = policy.request_approval("Bash", {"command": "rm -r out"}, "deleting files", session="other-chat")
    code = policy.get(aid)["code"]
    assert chat_engine.approval_preflight("telegram:5", f"deny {code}")[0] == "reply"
    assert policy.get(aid)["state"] == "denied"
    kind, text = chat_engine.approval_preflight("telegram:5", f"approve {code}")
    assert kind == "reply" and "no longer open" in text
    kind, text = chat_engine.approval_preflight("telegram:5", "approve QQQQ")
    assert kind == "reply" and "don't have an approval" in text


def test_plain_yes_grants_this_chats_pending_and_nothing_else():
    assert chat_engine.approval_preflight("telegram:5", "yes") is None
    assert chat_engine.approval_preflight("telegram:5", "yes please tell me more about it") is None
    aid = policy.request_approval("Write", {"file_path": "a.txt"}, "writing", session="telegram:5")
    kind, text = chat_engine.approval_preflight("telegram:5", "yes")
    assert kind == "note" and policy.get(aid)["state"] == "granted"


def test_plain_yes_with_background_work_waiting_asks_for_the_code():
    policy.request_approval("Bash", {"command": "git push"}, "pushing", session="objective-abc")
    kind, text = chat_engine.approval_preflight("telegram:5", "yes", prev_msg_at=0.0)
    assert kind == "reply" and "approve" in text


# ---- background jobs ---------------------------------------------------------------------------------------

class Res:
    def __init__(self, text, ok=True):
        self.text, self.ok, self.error, self.approvals = text, ok, "" if ok else "it broke", []


def test_background_task_tool_queues_a_core_job(monkeypatch):
    monkeypatch.setenv("WAGGLE_CHAT", "telegram:5")
    out = json.loads(chat_jobs._t_background_task({"task": "Summarise the repo", "agent": "researcher"}))
    job = jobs.get(out["job"])
    assert out["started"] and job["kind"] == chat_jobs.JOB_KIND and job["state"] == "queued"
    assert job["payload"]["chat"] == "telegram:5" and job["payload"]["agent"] == "researcher"
    assert job["idem_key"] == f"{chat_jobs.JOB_KIND}:{job['payload']['key']}"
    monkeypatch.delenv("WAGGLE_CHAT")
    assert "only works from a chat" in chat_jobs._t_background_task({"task": "x"})
    assert "Give the task" in chat_jobs._t_background_task({"task": " "})


def test_register_core_and_a_job_delivers_its_result_once(monkeypatch):
    assert chat_engine.register_core() == [chat_jobs.JOB_KIND, chat_jobs.REAP_KIND, chat_jobs.PREWARM_KIND]
    runs = []
    monkeypatch.setattr("waggle.agents.run", lambda *a, **k: runs.append(a) or Res("The summary."))
    jid, key = chat_jobs.start("telegram:5", "Summarise the repo")
    done = jobs.run_one()
    assert done["id"] == jid and done["state"] == "done" and done["result"]["ok"] is True
    item = outbox.get(done["result"]["outbox"])
    assert item["idem_key"] == f"{chat_jobs.JOB_KIND}:{key}" and item["chat"] == "telegram:5"
    assert item["body"].startswith("Background job done: Summarise the repo") and "The summary." in item["body"]
    assert item["meta"]["kind"] == "background" and len(runs) == 1
    again = chat_jobs.run_background(jobs.get(jid)["payload"])  # a re-run (e.g. after a restart)
    assert again["rerun"] and len(runs) == 1 and len(outbox.list()) == 1
    status = json.loads(chat_jobs._t_background_status({}))
    assert status[0]["job"] == jid and status[0]["state"] == "done" and status[0]["ok"] is True


def test_job_interrupted_by_a_core_restart_runs_again_and_delivers_once(monkeypatch):
    chat_jobs.register_core()
    monkeypatch.setattr("waggle.agents.run", lambda *a, **k: Res("Result after restart."))
    jid, key = chat_jobs.start("telegram:5", "Long task", now=0)
    claimed = jobs.claim("core:dead:1", now=1)  # the old core took it, then died mid-run
    assert claimed["id"] == jid
    monkeypatch.setattr(procs, "same_process", lambda pid, ctime: False)
    assert jobs.recover(now=1 + chat_jobs.JOB_LEASE_S + 5)["requeued"] == 1
    done = jobs.run_one(now=1 + chat_jobs.JOB_LEASE_S + 6)
    assert done["state"] == "done" and done["attempts"] == 1
    rows = outbox.list()
    assert len(rows) == 1 and "Result after restart." in rows[0]["body"]


def test_crashed_agent_still_tells_the_user():
    out = chat_jobs.run_background({"chat": "telegram:5", "task": "Do it", "key": "k1"},
                                   runner=lambda payload: (_ for _ in ()).throw(RuntimeError("agent exploded")))
    item = outbox.get(out["outbox"])
    assert out["ok"] is False and item["body"].startswith("Background job didn't finish: Do it")
    assert "agent exploded" in item["body"]
    failed = chat_jobs.run_background({"chat": "telegram:5", "task": "Other", "key": "k2"},
                                      runner=lambda payload: Res("", ok=False))
    assert "it broke" in outbox.get(failed["outbox"])["body"]


def test_reap_job(monkeypatch):
    chat_jobs.register_core()
    monkeypatch.setattr(chat_engine.pool(), "reap", lambda now=None, keep=(): ["telegram:9"])
    jobs.enqueue(chat_jobs.REAP_KIND)
    assert jobs.run_one()["result"] == {"stopped": ["telegram:9"], "rolled": []}  # rolled: new-day sessions started


# ---- handle_inbound --------------------------------------------------------------------------------------------

def inbound(settings, text="hello", api=None, script=None, **kw):
    settings.edit_interval_s = 0.05
    clock = FakeClock()
    api = api or FakeBotApi(clock=clock)
    chat_engine.set_delivery_factory("telegram", lambda address: BotApiDelivery(address, api, clock=clock,
                                                                                sleep=clock.sleep))
    fake = FakeClaude(script)
    sess = ChatSession("telegram:42", settings=settings, popen=fake, claude="claude")
    kw.setdefault("session", sess)
    rec = chat_engine.handle_inbound("telegram:42", text, settings=settings, **kw)
    return rec, api, fake


def test_handle_inbound_answers_through_the_bot_api(settings, monkeypatch):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "<waggle_context>\nnotes\n</waggle_context>")
    rec, api, fake = inbound(settings, "what's the plan?")
    assert rec.delivered and rec.delivery == "telegram" and rec.outcome == "ok" and rec.origin == "user"
    assert [api.messages[i] for i in rec.message_ids] == ["Hello from Claude."]
    assert api.calls[0][0] == "send_chat_action" and api.calls[1][2] == "42"
    assert fake.procs[0].inputs[0].endswith("what's the plan?") and "<waggle_context>" in fake.procs[0].inputs[0]
    r = traces.get(rec.trace_id)
    assert r["platform"] == "telegram" and json.loads(r["message_ids"]) == rec.message_ids
    from waggle import chat_store
    row = chat_store.get_chat("telegram:42")
    assert row["address"] == "42" and row["platform"] == "telegram" and row["last_user"] > 0


def test_handle_inbound_approval_reply_needs_no_model(settings):
    aid = policy.request_approval("Bash", {"command": "rm -r x"}, "deleting", session="someone-else")
    code = policy.get(aid)["code"]
    rec, api, fake = inbound(settings, f"deny {code}")
    assert fake.procs == [] and rec.answered_by == "none" and rec.outcome == "ok" and rec.delivered
    assert "Denied" in api.messages[rec.message_ids[0]] and traces.get(rec.trace_id)["route"] == "approval"


def test_handle_inbound_voice_without_a_transcript_is_an_honest_failure(settings):
    rec, api, fake = inbound(settings, "", kind="voice")
    assert fake.procs == [] and rec.outcome == "failed" and "could not be transcribed" in rec.reply


def _stt(monkeypatch, text="what's the plan for tomorrow", ok=True, error=""):
    seen = []

    def fake(path, **kw):
        seen.append(path)
        return {"ok": ok, "text": text if ok else "", "error": error, "seconds": 1.5, "load_s": 0.5, "audio_s": 3.2,
                "source": "fake whisper"}
    monkeypatch.setattr(chat_engine.stt, "transcribe", fake)
    return seen


def test_a_voice_note_is_transcribed_here_and_answered(settings, monkeypatch):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    seen = _stt(monkeypatch, "next thing on the ethics robotics bot")
    rec, api, fake = inbound(settings, "", kind="voice", audio="C:/in/note.ogg")
    assert seen == ["C:/in/note.ogg"] and rec.delivered and rec.outcome == "ok"
    assert "next thing on the FTC robotics bot" in fake.procs[0].inputs[0]  # known mishearings fixed
    extra = json.loads(traces.get(rec.trace_id)["extra"])["voice_turn"]
    assert extra["transcript_source"] == "fake whisper" and extra["stt_s"] == 1.5 and extra["stt_ok"] is True


def test_a_voice_note_that_cannot_be_transcribed_gets_an_honest_reply(settings, monkeypatch):
    _stt(monkeypatch, ok=False, error="no speech was recognised")
    rec, api, fake = inbound(settings, "", kind="voice", audio="C:/in/note.ogg")
    assert fake.procs == [] and rec.outcome == "failed" and rec.reply.startswith("I couldn't make out")
    extra = json.loads(traces.get(rec.trace_id)["extra"])["voice_turn"]
    assert extra["stt_ok"] is False and "no speech" in extra["stt_error"]


def test_on_inbound_voice_hands_the_audio_file_to_the_turn(settings, monkeypatch):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    seen = _stt(monkeypatch)
    fake = FakeClaude(None)
    sess = ChatSession("telegram:42", settings=settings, popen=fake, claude="claude")
    monkeypatch.setattr(chat_engine.pool(settings), "get", lambda chat: sess)
    api = FakeBotApi()
    chat_engine.set_delivery_factory("telegram", lambda address: BotApiDelivery(address, api))
    out = chat_engine.on_inbound({"chat": "telegram:42", "address": "42", "text": "", "kind": "voice",
                                  "media": [{"path": "C:/in/note.ogg", "kind": "audio"}], "platform": "telegram",
                                  "origin": "user", "turn_id": "v-1"})
    assert seen == ["C:/in/note.ogg"] and out["delivered"] and "C:/in/note.ogg" not in fake.procs[0].inputs[0]


def test_handle_inbound_media_and_origin(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff-jpeg")
    rec, api, fake = inbound(settings, "what is this?", kind="photo", media=[{"path": str(photo), "kind": "photo"}],
                             origin="test")
    sent = fake.procs[0].inputs[0]  # the file is saved into the chat's media folder and described in a note
    assert sent.startswith("what is this?") and '<attachment path="' in sent and 'kind="image"' in sent
    assert rec.origin == "test"
    assert traces.all_traces() == [] and traces.get(rec.trace_id)["origin"] == "test"
    with pytest.raises(ValueError):
        chat_engine.handle_inbound("telegram:42", "x", origin="robot", delivery=FakeDelivery())


def test_handle_inbound_failed_send_is_queued_for_the_core_sender(settings, monkeypatch):
    from engine_fakes import TelegramError
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    api = FakeBotApi()
    api.fail["send_message"] = [TelegramError("Unauthorized", "auth")] * 3
    rec, _, _ = inbound(settings, api=api)
    item = outbox.get(rec.outbox_id)
    assert not rec.delivered and item["meta"]["address"] == "42" and item["idem_key"] == f"turn:{rec.turn_id}"
    assert rec.outcome == "degraded" and "Unauthorized" in rec.delivery_error


def test_engine_is_off_by_default_in_the_gateway():
    assert Settings().engine_enabled is False


def test_unknown_platform_needs_a_delivery():
    with pytest.raises(ValueError):
        chat_engine.delivery_for("x:1", "smoke-signals")
    assert chat_engine.address_for("telegram:123", "telegram") == "123"
    assert chat_engine.address_for("123", "telegram") == "123"


# ---- Step 1: answers by voice, and asks that go stale ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["Yes, go ahead.", "Yeah do it", "Yes go ahead and do it, Waggle.", "sure, go for it",
                                  "that's fine, go ahead", "Okay sure"])
def test_a_spoken_yes_grants_this_chats_pending(text):
    aid = policy.request_approval("Bash", {"command": "pip install requests"}, "installing third-party code",
                                  session="telegram:5")
    kind, _ = chat_engine.approval_preflight("telegram:5", text)
    assert kind == "note" and policy.get(aid)["state"] == "granted"


@pytest.mark.parametrize("text", ["yes but rename it first", "no idea", "no, go ahead", "yes I think so, why?"])
def test_mixed_or_longer_answers_go_to_claude(text):
    aid = policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    assert chat_engine.approval_preflight("telegram:5", text) is None
    assert policy.get(aid)["state"] == "pending"


@pytest.mark.parametrize("text", ["no", "No, don't.", "nope, not now", "never mind", "cancel that"])
def test_a_plain_no_denies_this_chats_pending_and_tells_claude(text):
    aid = policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    other = policy.request_approval("Bash", {"command": "git push"}, "pushing", session="objective-abc")
    kind, note = chat_engine.approval_preflight("telegram:5", text)
    assert kind == "note" and "said no" in note and policy.get(aid)["state"] == "denied"
    assert policy.get(other)["state"] == "pending"  # background work is never answered by a bare no


def test_a_plain_no_with_nothing_pending_is_just_a_message():
    assert chat_engine.approval_preflight("telegram:5", "no") is None


def test_an_unsure_transcript_grants_nothing():
    aid = policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    kind, text = chat_engine.approval_preflight("telegram:5", "yes go ahead", heard_unsure=True)
    assert kind == "reply" and "wasn't sure" in text and policy.get(aid)["state"] == "pending"


def test_stale_approvals_expire_once_with_one_line_to_the_owner(settings):
    from waggle import duties_core
    settings.engine_chats = ["8273865620"]
    settings.save()
    t0 = 1_800_000_000.0
    a = policy.request_approval("Bash", {"command": "pip install x"}, "installing third-party code", session="telegram:5")
    b = policy.request_approval("Bash", {"command": "reg add HKCU"}, "changing the registry", session="telegram:5")
    import sqlite3
    from waggle.config import data_path
    con = sqlite3.connect(data_path("approvals.db"))
    con.execute("UPDATE approvals SET created=?", (t0,))
    con.commit()
    con.close()
    assert duties_core.approvals_job(now=t0 + 60)["expired"] == 0  # still open
    out = duties_core.approvals_job(now=t0 + policy.APPROVAL_TTL + 5)
    assert out["expired"] == 2 and out["sent"]
    rows = [r for r in outbox.list(limit=50) if r["origin"] == "approvals"]
    assert len(rows) == 1 and "2 requests got no answer" in rows[0]["body"] and "Nothing was done" in rows[0]["body"]
    assert policy.get(a)["state"] == "expired" and policy.get(b)["state"] == "expired"
    assert duties_core.approvals_job(now=t0 + policy.APPROVAL_TTL + 400) == {"expired": 0, "sent": False}
    kind, text = chat_engine.approval_preflight("telegram:5", f"approve {policy.get(a)['code']}")
    assert kind == "reply" and "no longer open" in text  # a late yes revives nothing


def test_leftovers_expired_long_ago_close_without_a_message(settings):
    from waggle import duties_core
    t0 = 1_800_000_000.0
    policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    import sqlite3
    from waggle.config import data_path
    con = sqlite3.connect(data_path("approvals.db"))
    con.execute("UPDATE approvals SET created=?", (t0,))
    con.commit()
    con.close()
    assert duties_core.approvals_job(now=t0 + 3 * 86400) == {"expired": 1, "sent": False}


def test_consent_inside_a_longer_spoken_reply_grants_the_fresh_ask():
    # trace 206 (2026-11-21): the owner's voice reply that the plain-yes check missed
    aid = policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    said = "Where's the approval? Where can I do it? But I give it for permission, go ahead."
    kind, _ = chat_engine.approval_preflight("telegram:5", said)
    assert kind == "note" and policy.get(aid)["state"] == "granted"


@pytest.mark.parametrize("said", ["don't go ahead", "wait, go ahead later", "should we go ahead first?",
                                  "tell me about the approval"])
def test_consent_words_with_doubt_or_refusal_grant_nothing(said):
    aid = policy.request_approval("Bash", {"command": "pip install x"}, "installing", session="telegram:5")
    pre = chat_engine.approval_preflight("telegram:5", said)
    assert pre is None or pre[0] != "note" or "approved" not in pre[1]
    assert policy.get(aid)["state"] == "pending"


def test_consent_in_a_sentence_needs_a_fresh_ask_from_this_chat():
    aid = policy.request_approval("Bash", {"command": "git push"}, "pushing", session="objective-abc")
    assert chat_engine.approval_preflight("telegram:5", "Sure, you have my permission, go ahead with it all") is None
    assert policy.get(aid)["state"] == "pending"


def test_an_ask_the_owner_never_saw_expires_without_a_message(settings):
    from waggle import duties_core
    t0 = 1_800_000_000.0
    policy.request_approval("StructuredOutput", {"events": []}, "an unfamiliar tool", session="")
    import sqlite3
    from waggle.config import data_path
    con = sqlite3.connect(data_path("approvals.db"))
    con.execute("UPDATE approvals SET created=?", (t0,))
    con.commit()
    con.close()
    assert duties_core.approvals_job(now=t0 + policy.APPROVAL_TTL + 5) == {"expired": 1, "sent": False}
