"""The engine behind real Telegram messages: the admission hook, the core's inbound handler, typing, second
attempts, pre-warming, reaping, chat rows for the sender, and reminders / calls as chat tools."""
import http.server
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from engine_fakes import FakeBotApi, FakeClaude, FakeClock, FakeDelivery, TelegramError, ev_text

from waggle import chat_engine, chat_jobs, chat_store, traces
from waggle.chat_session import ChatSession, SessionPool
from waggle.core import jobs, outbox, store, timers
from waggle.delivery import BotApiDelivery, Typing


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    jobs._handlers.clear()
    monkeypatch.setattr(chat_engine, "build_context", lambda *a, **k: "")
    yield
    jobs._handlers.clear()
    chat_engine.set_delivery_factory("telegram", None)
    chat_engine.set_pool(None)
    store.close_all()


def use_fakes(settings, script=None, api=None):
    settings.edit_interval_s = 0.05
    settings.save()
    clock = FakeClock()
    api = api or FakeBotApi(clock=clock)
    chat_engine.set_delivery_factory("telegram", lambda address: BotApiDelivery(address, api, clock=clock,
                                                                                sleep=clock.sleep))
    fake = FakeClaude(script)
    chat_engine.set_pool(SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake,
                                                                      claude="claude"), settings=settings))
    return api, fake


def msg(**kw):
    base = {"turn_id": "t-1", "chat": "telegram:42", "address": "42", "text": "what's next?", "kind": "text",
            "media": None, "platform": "telegram", "origin": "user", "received_ts": time.time(), "attempt": 1}
    return {**base, **kw}


# ---- the core's inbound handler ----------------------------------------------------------------------------

def test_inbound_message_is_answered_through_the_bot_api(settings):
    api, fake = use_fakes(settings)
    out = chat_engine.on_inbound(msg())
    rec = traces.by_turn_id("t-1")
    assert out["delivered"] and out["outcome"] == "ok" and rec["id"] == out["trace_id"]
    assert api.calls[0][0] == "send_chat_action" and api.calls[0][2] == "42"
    sent = [c for c in api.calls if c[0] == "send_message"]
    ids = json.loads(rec["message_ids"])
    assert sent[0][2] == "42" and [api.messages[i] for i in ids] == ["Hello from Claude."] and rec["origin"] == "user"
    row = chat_store.get_chat("telegram:42")
    assert row["address"] == "42" and row["platform"] == "telegram"


def test_typing_stays_alive_until_the_first_words(settings, monkeypatch):
    monkeypatch.setattr(chat_engine, "TYPING_EVERY_S", 0.05)

    def script(proc, text):
        proc.answer("Slow to start, then fast.", pieces=2, delay=0.4)
    api, _ = use_fakes(settings, script)
    chat_engine.on_inbound(msg())
    kinds = [c[0] for c in api.calls]
    first_post = kinds.index("send_message")
    assert kinds[0] == "send_chat_action" and kinds[:first_post].count("send_chat_action") >= 2
    time.sleep(0.15)
    assert "send_chat_action" not in [c[0] for c in api.calls][first_post:]


def test_typing_keeper_alone():
    d = FakeDelivery()
    k = Typing(d, every=0.02).start()
    time.sleep(0.09)
    k.stop()
    n = d.typed
    time.sleep(0.06)
    assert n >= 3 and d.typed == n and not k.running


def test_second_attempt_checks_before_redoing_and_tells_the_user(settings):
    api, fake = use_fakes(settings)
    out = chat_engine.on_inbound(msg(turn_id="t-2", attempt=2))
    sent_to_model = fake.procs[0].inputs[0]
    assert "restarted while you were answering" in sent_to_model and "check what was" in sent_to_model
    reply = traces.by_turn_id("t-2")["reply"]
    assert reply.startswith("Hello from Claude.") and "A restart interrupted my first try" in reply
    assert out["delivered"] and traces.get(out["trace_id"])["extra"] and \
        json.loads(traces.get(out["trace_id"])["extra"])["session"]["attempt"] == 2


def test_second_attempt_of_a_turn_already_delivered_is_not_answered_again(settings):
    api, fake = use_fakes(settings)
    chat_engine.on_inbound(msg(turn_id="t-3"))
    sends = len([c for c in api.calls if c[0] == "send_message"])
    out = chat_engine.on_inbound(msg(turn_id="t-3", attempt=2))
    assert out["delivered"] and len([c for c in api.calls if c[0] == "send_message"]) == sends
    assert len(fake.procs[0].inputs) == 1


def test_failed_reply_has_a_chat_row_for_the_sender(settings):
    api = FakeBotApi()
    api.fail["send_message"] = [TelegramError("down", "permanent")] * 3
    use_fakes(settings, api=api)
    out = chat_engine.on_inbound(msg(chat="telegram:77", address="77", turn_id="t-4"))
    item = outbox.get(out["outbox"])
    assert item["chat"] == "telegram:77" and chat_store.get_chat("telegram:77")["address"] == "77"


def test_unknown_origin_is_never_the_user(settings):
    use_fakes(settings)
    chat_engine.on_inbound(msg(turn_id="t-5", origin="mystery"))
    assert traces.by_turn_id("t-5")["origin"] == "test"


def test_turn_origin_reaches_the_tools_environment(settings):
    api, fake = use_fakes(settings)
    chat_engine.on_inbound(msg(turn_id="t-6"))
    env = fake.procs[0].kwargs["env"]
    assert env["WAGGLE_TURN_ORIGIN"] == "user" and "WAGGLE_ORIGIN" not in env
    chat_engine.on_inbound(msg(turn_id="t-7", origin="test"))
    assert len(fake.procs) == 2 and fake.procs[1].kwargs["env"]["WAGGLE_ORIGIN"] == "test"
    cfg = json.loads(Path(fake.procs[1].arg("--mcp-config")).read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["waggle"]["env"]["WAGGLE_TURN_ORIGIN"] == "test"


# ---- register_core: inbound, reap timer, pre-warm ------------------------------------------------------------

def test_register_core_registers_inbound_timer_and_one_prewarm_per_core_start(settings, monkeypatch):
    registered = []
    from waggle.core import inbound
    monkeypatch.setattr(inbound, "register", registered.append)
    from waggle.core import settings as core_settings
    core_settings.path("core.pid").write_text(json.dumps({"pid": 123, "ctime": 1, "started": 1000.5}), "utf-8")
    kinds = chat_jobs.register_core()
    chat_jobs.register_core()  # a detached job runner loads the module too
    assert registered == [chat_engine.on_inbound, chat_engine.on_inbound]
    assert kinds == ["chat.background", "chat.reap", "chat.prewarm"]
    t = timers.get("chat.reap")
    assert t["kind"] == "interval" and t["catch_up"] == "skip" and t["job_kind"] == "chat.reap"
    assert [j["idem_key"] for j in jobs.list(kind="chat.prewarm")] == ["chat.prewarm:123:1000.5"]


def test_register_core_without_inbound_or_core_still_registers_jobs(settings):
    assert chat_jobs.register_core() == ["chat.background", "chat.reap", "chat.prewarm"]
    assert jobs.list(kind="chat.prewarm") == []


def test_prewarm_starts_every_engine_chat_and_the_chats_seen_in_the_last_day(settings):
    api, fake = use_fakes(settings)
    now = time.time()
    chat_store.touch_chat("telegram:42", "telegram", address="42", user=True, now=now - 3600)
    chat_store.touch_chat("telegram:43", "telegram", address="43", user=True, now=now - 60)  # not an engine chat
    chat_store.touch_chat("telegram:45", "telegram", address="45", user=True, now=now - 3 * 86400)
    settings.engine_chats = ["42", "44"]  # 44 has no row at all: the owner who has not written yet
    settings.save()
    out = chat_engine.prewarm(now=now)
    started = {"started": True, "resumed": False}
    assert out == {"telegram:42": started, "telegram:44": started, "telegram:43": started}
    assert len(fake.procs) == 3 and all(p.inputs == [] for p in fake.procs)  # nothing was sent: just warm
    assert chat_store.get_chat("telegram:44")["address"] == "44"  # the row the sender resolves the chat through
    chat_engine.on_inbound(msg(turn_id="t-8"))
    assert len(fake.procs) == 3 and any(p.inputs for p in fake.procs) and traces.by_turn_id("t-8")["turn_outcome"] == "ok"


def test_prewarm_of_an_owner_without_a_row_and_nobody_else(settings):
    api, fake = use_fakes(settings)
    settings.engine_chats = ["42"]
    settings.save()
    assert chat_store.get_chat("telegram:42") is None
    out = chat_engine.prewarm(now=time.time())
    assert out == {"telegram:42": {"started": True, "resumed": False}} and len(fake.procs) == 1
    # the core's pre-warm job does the same, and the reaper keeps that chat warm although it has no row yet
    assert chat_jobs.prewarm()["warmed"] == {"telegram:42": {"started": False, "resumed": False}}
    assert chat_engine.engine_chat_keys(settings) == {"telegram:42"}


def test_reap_keeps_engine_chats_warm_longer(settings):
    settings.chat_idle_s = 10
    fake = FakeClaude()
    pool = SessionPool(factory=lambda chat: ChatSession(chat, settings=settings, popen=fake, claude="claude"),
                       settings=settings)
    owner, other = pool.get("telegram:42"), pool.get("telegram:43")
    owner.warm()
    other.warm()
    now = time.time()
    owner.last_used = other.last_used = now - 600
    assert pool.reap(now=now, keep={"telegram:42"}) == ["telegram:43"]
    owner.last_used = now - 13 * 3600
    assert pool.reap(now=now, keep={"telegram:42"}) == ["telegram:42"]


def test_reap_job_keeps_the_owner_chat(settings, monkeypatch):
    settings.engine_chats = ["42"]
    settings.save()
    chat_store.touch_chat("telegram:42", "telegram", address="42")
    seen = {}
    monkeypatch.setattr(chat_engine.pool(), "reap", lambda now=None, keep=(): seen.setdefault("keep", keep) and [])
    chat_jobs.reap()
    assert seen["keep"] == {"telegram:42"}


def test_engine_switch_per_chat(settings, capsys):
    assert chat_engine.engine_chats() == set()
    assert chat_engine.main(["engine", "on", "42"]) == 0 and chat_engine.engine_chats() == {"42"}
    chat_engine.main(["engine", "on", "42"])
    assert chat_engine.engine_chats() == {"42"}
    assert chat_engine.main(["engine", "off", "42"]) == 0 and chat_engine.engine_chats() == set()
    assert chat_engine.main(["engine", "status"]) == 0 and "none" in capsys.readouterr().out
    assert chat_engine.main(["nonsense"]) == 64


# ---- the admission hook -----------------------------------------------------------------------------------

def _plugin():
    path = Path(__file__).parents[1] / "hermes_plugin" / "waggle" / "__init__.py"
    spec = importlib.util.spec_from_file_location("wp_admission", path)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    return p


class FakeCore(http.server.BaseHTTPRequestHandler):
    status, delay, seen = 202, 0.0, []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeCore.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
        time.sleep(FakeCore.delay)
        out = json.dumps({"turn_id": "core-1", "duplicate": False}).encode()
        self.send_response(FakeCore.status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def core(monkeypatch, settings):
    FakeCore.status, FakeCore.delay, FakeCore.seen = 202, 0.0, []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("WAGGLE_CORE_PORT", str(server.server_address[1]))
    from waggle.core import settings as core_settings
    core_settings.path("core.token").write_text("secret-token", encoding="utf-8")
    settings.engine_chats = ["42"]
    settings.save()
    yield FakeCore
    server.shutdown()


def event(p, mid, mtype="text", media=()):
    p.on_pre_gateway_dispatch(event=types.SimpleNamespace(message_id=mid, message_type=types.SimpleNamespace(value=mtype),
                                                          media_urls=list(media)))


def admit(p, text="what's on today?", mid="m1", chat_id="42", **src):
    source = {"platform": "telegram", "chat_id": chat_id, "user_id": chat_id, "chat_type": "dm", **src}
    return p.on_gateway_admission(session_key="k", platform="telegram", source=source, message_id=mid, text=text)


def test_typed_text_in_the_engine_chat_goes_to_the_core(core):
    p = _plugin()
    event(p, "m1")
    assert admit(p) == {"action": "handled"}
    sent = core.seen[0]
    assert sent["path"] == "/turn" and sent["auth"] == "Bearer secret-token"
    assert sent["body"] == {"chat": "telegram:42", "address": "42", "text": "what's on today?", "kind": "text",
                            "media": [], "platform": "telegram", "origin": "user", "dedupe_key": "telegram:42:m1"}


@pytest.mark.parametrize("text,mtype,media,src,why", [
    ("hello", "voice", (), {}, "caption"),
    ("", "audio", ("C:/in/song.mp3",), {}, "not typed text"),
    ("look at this", "photo", ("C:/in/p.jpg",), {}, "not typed text"),
    ("caption", "text", ("C:/in/doc.pdf",), {}, "not typed text"),
    ("/status", "text", (), {}, "command"),
    ("approve ABCD", "text", (), {}, "approval reply"),
    ("yes", "text", (), {}, "approval reply"),
    ("go ahead", "text", (), {}, "approval reply"),
    ("objective: tidy the vault notes", "text", (), {}, "objective"),
    ("useful 0123456789ab", "text", (), {}, "feedback"),
    ('"hello"\n[User sent audio: C:/in/a.ogg]', "text", (), {}, "voice"),
    ("hello", "text", (), {"chat_type": "group"}, "not the direct chat"),
    ("hello", "text", (), {"thread_id": "9"}, "not the direct chat"),
    ("hello", "text", (), {"user_id": "99"}, "owner"),
])
def test_everything_else_stays_on_the_old_path(core, text, mtype, media, src, why):
    p = _plugin()
    event(p, "m2", mtype, media)
    assert admit(p, text=text, mid="m2", **src) is None and core.seen == []
    event(p, "m3", mtype, media)
    route, reason = p.engine_route("telegram", {"chat_id": "42", "user_id": "42", "chat_type": "dm", **src}, "m3",
                                   text)
    assert route == "old" and why in reason


def _note(tmp_path, name="voice.ogg", data=b"OggS-fake"):
    f = tmp_path / name
    f.write_bytes(data)
    return str(f)


def test_a_voice_note_in_the_engine_chat_goes_to_the_core_as_audio(core, tmp_path):
    p = _plugin()
    path = _note(tmp_path)
    event(p, "v1", "voice", [path])
    assert admit(p, text="", mid="v1") == {"action": "handled"}
    assert core.seen[0]["body"] == {"chat": "telegram:42", "address": "42", "text": "", "kind": "voice",
                                    "media": [{"path": path, "kind": "audio"}], "platform": "telegram",
                                    "origin": "user", "dedupe_key": "telegram:42:v1"}


def test_a_voice_note_the_core_refuses_stays_on_the_old_path(core, tmp_path):
    core.status = 503
    p = _plugin()
    event(p, "v2", "voice", [_note(tmp_path)])
    assert admit(p, text="", mid="v2") is None and len(core.seen) == 1


@pytest.mark.parametrize("media,text,why", [
    ((), "", "several files"),  # no file at all
    (("a.ogg", "b.ogg"), "", "several files"),
    (("one.ogg",), "a caption", "caption"),
    (("missing.ogg",), "", "file missing"),
    (("notes.txt",), "", "file missing"),
])
def test_voice_notes_without_one_usable_audio_file_stay_old(core, tmp_path, media, text, why):
    p = _plugin()
    paths = [_note(tmp_path, m) if m in ("a.ogg", "b.ogg", "one.ogg", "notes.txt") else str(tmp_path / m) for m in media]
    event(p, "v3", "voice", paths)
    route, reason = p.engine_route("telegram", {"chat_id": "42", "user_id": "42", "chat_type": "dm"}, "v3", text)
    assert route == "old" and why in reason and core.seen == []


def test_voice_note_in_other_chats_and_groups_stays_old(core, tmp_path):
    p = _plugin()
    path = _note(tmp_path)
    event(p, "v4", "voice", [path])
    assert admit(p, text="", mid="v4", chat_id="43") is None
    event(p, "v5", "voice", [path])
    assert admit(p, text="", mid="v5", chat_type="group") is None
    assert core.seen == []


def test_other_chats_unknown_types_and_other_platforms_stay_old(core):
    p = _plugin()
    event(p, "m4")
    assert admit(p, chat_id="43", mid="m4") is None
    assert admit(p, mid="never-seen") is None
    assert p.on_gateway_admission(platform="whatsapp", source={"chat_id": "42"}, message_id="m4", text="hi") is None
    assert core.seen == []


def test_engine_off_by_default(settings):
    p = _plugin()
    event(p, "m5")
    assert admit(p, mid="m5") is None


@pytest.mark.parametrize("status", [503, 500, 401])
def test_core_refusal_falls_back_to_the_old_path(core, status):
    core.status = status
    p = _plugin()
    event(p, "m6")
    assert admit(p, mid="m6") is None and len(core.seen) == 1


def test_slow_or_absent_core_falls_back(core, monkeypatch):
    p = _plugin()
    core.delay = 0.5
    ok, detail = p.post_turn("42", "hi", "m7", timeout=0.2)
    assert not ok and "timed out" in detail.lower()
    monkeypatch.setenv("WAGGLE_CORE_PORT", "1")  # nothing listens there
    event(p, "m8")
    assert admit(p, mid="m8") is None


def test_no_core_token_means_no_engine(core):
    from waggle.core import settings as core_settings
    core_settings.path("core.token").unlink()
    p = _plugin()
    event(p, "m9")
    assert admit(p, mid="m9") is None and core.seen == []


# ---- reminders and calls as chat tools -----------------------------------------------------------------------

def call_tool(name, args):
    from waggle import waggle_mcp
    out = waggle_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args}})
    return out["result"]["content"][0]["text"]


def test_remind_me_in_two_minutes_ends_as_a_core_timer_and_an_outbox_row(settings, monkeypatch):
    from waggle import tick, waggle_mcp
    now = 1_800_000_000.0
    monkeypatch.setattr(waggle_mcp, "_now", lambda: now)
    monkeypatch.setenv("WAGGLE_CHAT", "telegram:42")
    monkeypatch.setenv("WAGGLE_TURN_ORIGIN", "user")
    out = json.loads(call_tool("remind_me", {"text": "stand up", "at": "in 2 minutes"}))
    t = timers.get(f"reminder:{out['reminder']}")
    assert t["kind"] == "once" and t["job_kind"] == "outreach.reminder" and abs(t["next_run"] - (now + 120)) < 1
    timers.fire_due(now=now + 60)
    assert jobs.list(kind="outreach.reminder") == []
    timers.fire_due(now=now + 121)
    tick.register_jobs()
    done = jobs.run_one(kinds=["outreach.reminder"], now=now + 122)
    assert done["state"] == "done"
    row = outbox.get(done["result"]["outbox"])
    assert row["body"] == "⏰ stand up" and row["chat"] == "telegram:42" and row["state"] == "queued"


def test_reminder_list_cancel_and_bad_times(settings, monkeypatch):
    from waggle import waggle_mcp
    now = 1_800_000_000.0
    monkeypatch.setattr(waggle_mcp, "_now", lambda: now)
    monkeypatch.setenv("WAGGLE_CHAT", "telegram:42")
    fid = json.loads(call_tool("remind_me", {"text": "charge the batteries", "in_minutes": 30}))["reminder"]
    assert [r["id"] for r in json.loads(call_tool("list_reminders", {}))] == [fid]
    assert json.loads(call_tool("cancel_reminder", {"id": fid}))["cancelled"] is True
    assert timers.get(f"reminder:{fid}") is None and json.loads(call_tool("list_reminders", {})) == []
    assert call_tool("remind_me", {"text": "x", "at": "whenever"}).startswith("Not scheduled")
    assert call_tool("remind_me", {"text": "x", "in_minutes": -10}).startswith("Not scheduled")
    assert call_tool("remind_me", {"at": "in 2 minutes"}).startswith("Say what")


def test_test_chats_reminders_carry_their_origin(settings, monkeypatch):
    from waggle import followups, waggle_mcp
    monkeypatch.setattr(waggle_mcp, "_now", lambda: 1_800_000_000.0)
    monkeypatch.setenv("WAGGLE_CHAT", "test:1")
    monkeypatch.setenv("WAGGLE_TURN_ORIGIN", "test")
    fid = json.loads(call_tool("remind_me", {"text": "x", "in_minutes": 1}))["reminder"]
    assert followups.get(fid)["origin"] == "test"


def test_schedule_call_is_a_core_timer(settings, monkeypatch):
    from waggle import waggle_mcp
    now = 1_800_000_000.0
    monkeypatch.setattr(waggle_mcp, "_now", lambda: now)
    out = json.loads(call_tool("schedule_call", {"message": "the meet", "at": "2027-01-15T07:30"}))
    calls = [t for t in timers.list() if t["job_kind"] == "outreach.call"]
    assert len(calls) == 1 and out["message"] == "the meet"
    assert calls[0]["payload"]["message"] == "the meet" and calls[0]["payload"]["origin"] == "user"


def test_approval_replies_go_to_the_engine_only_for_its_own_approvals(core):
    from waggle import policy
    p = _plugin()
    mine = policy.request_approval("Bash", {"command": "rm -r build"}, "deleting", session="telegram:42")
    code = policy.get(mine)["code"]
    event(p, "a1")
    assert admit(p, text=f"approve {code}", mid="a1") == {"action": "handled"}
    event(p, "a2")
    assert admit(p, text="yes", mid="a2") == {"action": "handled"}  # still pending: the engine grants it
    other = policy.request_approval("Bash", {"command": "git push"}, "pushing", session="hermes-session-1")
    event(p, "a3")
    assert admit(p, text=f"approve {policy.get(other)['code']}", mid="a3") is None
    policy.grant(mine)
    event(p, "a4")
    assert admit(p, text="yes", mid="a4") is None  # nothing of the engine's pending: old path
    assert len(core.seen) == 2
