"""The Home page's chat (2026-11-25): dictation that is shown and checked before it is sent, the conversation so
far, Stop, the model the owner chose (kept until changed) and the live feed of what Waggle is doing."""
import json
import threading
import time

import pytest
from engine_fakes import FakeClaude, ev_result, ev_text

from waggle import chat_jobs, chat_store, comfort, config, home, liveturn, traces
from waggle.chat_session import ChatSession
from waggle.config import Settings
from waggle.core import store, voiceline


@pytest.fixture(autouse=True)
def _close_store():
    yield
    store.close_all()


def reply(code=200, headers=None, **body):
    return lambda path, data, ctype: (code, headers or {}, json.dumps(body).encode())


# ---- dictation: heard, shown, never sent ----

def test_hear_returns_the_words_and_runs_no_turn():
    seen = []

    def core(path, body, ctype):
        seen.append((path, ctype))
        return 200, {}, json.dumps({"text": "um so open the the Waggle repo", "ok": True, "confident": True}).encode()
    out = home.hear(b"x" * 4000, "audio/webm;codecs=opus", post=core)
    assert out["ok"] and seen == [("/voice/transcribe", "audio/webm")]  # the transcribe path only: nothing is sent
    assert "Waggle repo" in out["text"] and not out["text"].lower().startswith("um")  # fillers are taken out


def test_hear_waits_out_a_busy_recogniser_and_refuses_what_is_not_audio():
    answers = iter([(429, {}, b"{}"), (429, {}, b"{}"), (200, {}, json.dumps({"text": "hello there"}).encode())])
    out = home.hear(b"x" * 4000, "audio/webm", post=lambda *a: next(answers), sleep=lambda s: None)
    assert out == {"ok": True, "text": "hello there", "confident": False, "audio_s": None}
    assert home.hear(b"x" * 4000, "text/plain", post=reply(text="x"))["ok"] is False
    busy = home.hear(b"x" * 4000, "audio/webm", post=reply(429, error="another transcription is running"),
                     sleep=lambda s: None)
    assert busy["ok"] is False and busy["busy"] is True


def test_dictated_text_is_sent_as_checked_by_the_owner_and_typed_text_as_typed():
    paths = []

    def core(path, body, ctype):
        paths.append(path)
        return 200, {"X-Waggle-Turn": "abc123"}, json.dumps({"spoken": "Done.", "text": "Done, sir."}).encode()
    assert home.say("delete the old notes", post=core, dictated=True)["turn"] == "abc123"
    home.say("delete the old notes", post=core)
    assert paths == ["/voice?reply=json&client=dashmic", "/voice?reply=json&client=dash"]
    code, ctype, _, spoken, status, _, turn = home.say_aloud("what time is it", post=lambda p, b, c: (
        paths.append(p) or 200, {"Content-Type": "audio/wav", "X-Waggle-Spoken": "Four%20o%27clock.",
                                 "X-Waggle-Turn": "t9"}, b"RIFF"))
    assert (code, ctype, spoken, status, turn) == (200, "audio/wav", "Four o'clock.", "done", "t9")
    assert paths[-1] == "/voice?format=wav&client=dashmic"


def test_words_the_owner_checked_are_taken_as_written_with_full_authority(settings, monkeypatch):
    from waggle import chat_engine
    got = {}

    def fake_run_turn(chat, text, delivery, **kw):
        got.update(text=text, voice_info=kw.get("voice_info"))
        return chat_engine.TurnRecord(chat=chat, text=text, reply="ok", answered_by="x", delivered=True)
    monkeypatch.setattr(chat_engine, "run_turn", fake_run_turn)
    monkeypatch.setattr(chat_engine, "approval_preflight", lambda *a, **k: got.update(unsure=k.get("heard_unsure")))
    chat_engine.handle_inbound("telegram:1", "um, delete delete the old notes", kind="voice",
                               voice_source=voiceline.CLIENT_SOURCE["dashmic"], checked=True, origin="user",
                               delivery=voiceline.LineDelivery(audio=False))
    assert got["text"] == "um, delete delete the old notes"  # not cleaned again: the owner read exactly this
    assert got["voice_info"]["stt_confident"] is True and got["voice_info"]["checked"] is True
    assert got["unsure"] is False


# ---- Stop ----

def test_stop_asks_the_core_and_says_whether_a_turn_was_running():
    assert home.stop(post=reply(stopped=True)) == {"ok": True, "stopped": True, "error": ""}
    assert home.stop(post=reply(stopped=False))["stopped"] is False
    assert home.stop(post=reply(503, error="core down"))["ok"] is False


def test_interrupt_ends_the_running_turn_and_the_next_message_starts_a_fresh_session(settings):
    started = threading.Event()

    def script(proc, text):
        if text.endswith("long job"):
            proc.emit(ev_text(proc.session_id, "I started to")[:3])
            started.set()  # and then it keeps working: no result arrives
        else:
            proc.answer("back again")
    fake = FakeClaude(script)
    s = ChatSession("telegram:1", settings=settings, popen=fake, claude="claude",
                    transcript=lambda chat: "User: long job\nWaggle: I started to\n\n(Stopped, as you asked.)")
    assert s.interrupt() is False  # nothing is running
    out = {}
    t = threading.Thread(target=lambda: out.update(res=s.turn("long job", timeout=20)))
    t.start()
    assert started.wait(5)
    time.sleep(0.1)
    assert s.interrupt() is True
    t.join(10)
    res = out["res"]
    assert res.kind == "stopped" and not res.ok and not res.retried and len(fake.procs) == 1
    again = s.turn("hello")
    # never resumed: a session cut off mid-answer came back one message out of step on the real Claude (2026-11-25)
    assert again.ok and again.text == "back again" and again.fresh_session and not again.resumed
    assert "--resume" not in fake.procs[1].cmd and fake.procs[1].session_id != fake.procs[0].session_id
    assert "<earlier_conversation>" in fake.procs[1].inputs[0] and "Stopped" in fake.procs[1].inputs[0]  # kept


def test_an_answer_left_over_from_an_earlier_message_is_never_read_as_this_ones(settings):
    def script(proc, text):
        proc.answer(f"answer to {text}")
        if text == "one":
            proc.emit(ev_result(proc.session_id, "late leftover"))  # a second result nobody is waiting for
    fake = FakeClaude(script)
    s = ChatSession("telegram:1", settings=settings, popen=fake, claude="claude")
    assert s.turn("one").text == "answer to one"
    res = s.turn("two")
    assert res.ok and res.text == "answer to two"  # not "late leftover", and not one message late from here on
    assert s.turn("three").text == "answer to three"


def test_the_core_stops_the_owner_chat_only_when_its_turn_runs(settings, monkeypatch):
    from waggle import chat_engine
    from waggle.chat_session import SessionPool

    class Sess:
        def __init__(self):
            self.calls = 0

        def interrupt(self):
            self.calls += 1
            return True
    pool = SessionPool(factory=lambda chat: Sess())
    monkeypatch.setattr(chat_engine, "_pool", pool)
    monkeypatch.setattr(voiceline, "owner_chat", lambda: "telegram:7")
    assert voiceline.stop_turn() is False and not pool.sessions  # no session: nothing is made just to stop it
    sess = pool.get("telegram:7")
    assert voiceline.stop_turn() is True and sess.calls == 1


# ---- the model the owner chose ----

def owner():
    return {"owner": True, "origin": "user", "chat": "telegram:1"}


def test_the_owner_can_choose_who_answers_and_who_works_and_it_is_kept(settings):
    out = comfort.set_value("model", "Opus", owner())
    assert out["key"] == "chat_model" and out["new"] == "opus" and Settings.load().chat_model == "opus"
    assert comfort.set_value("job_model", "claude-fable-5-2", owner())["new"] == "fable"
    assert Settings.load().job_model == "fable"
    assert comfort.set_value("job_model", "auto", owner())["new"] == "" and Settings.load().job_model == ""
    with pytest.raises(comfort.SettingError):
        comfort.set_value("chat_model", "gpt-5", owner())
    with pytest.raises(comfort.SettingError):
        comfort.set_value("chat_model", "opus", {"owner": False})  # only in the owner's own turn
    got = comfort.get_all()
    assert got["settings"]["chat_model"]["value"] == "opus" and "fable" in got["models"]


def test_effort_is_a_setting_too_and_reaches_the_chat_and_the_jobs(isolated_home, monkeypatch):
    from waggle import agents
    assert comfort.set_value("effort", "High", owner())["key"] == "chat_effort" and Settings.load().chat_effort == "high"
    assert comfort.set_value("job_effort", "deep", owner())["new"] == "high" and Settings.load().job_effort == "high"
    with pytest.raises(comfort.SettingError):
        comfort.set_value("job_effort", "ludicrous", owner())
    assert home.brain()["chat_effort"] == "high" and home.set_brain({"chat_effort": "medium"})["chat_effort"] == "medium"
    fake = FakeClaude()
    s = ChatSession("telegram:1", popen=fake, claude="claude")
    s.turn("one")
    assert fake.procs[0].arg("--effort") == "medium"
    comfort.set_value("chat_effort", "high", owner())
    s.turn("two")
    assert len(fake.procs) == 2 and fake.procs[1].arg("--effort") == "high"  # the running chat follows the change
    seen = {}

    def fake_agent(task, **kw):
        seen.update(kw)
        return agents.runner.AgentResult(session="x", model="m")
    monkeypatch.setattr(agents.runner, "run", fake_agent)
    chat_jobs.run_background({"chat": "telegram:1", "task": "t", "agent": "coder", "hard": False, "origin": "user",
                              "key": "e1"})
    assert seen["effort"] == "high"
    comfort.set_value("job_effort", "low", owner())
    seen.clear()
    agents.run("coder", "t")  # no effort named: the agent runner's own default, as before
    assert "effort" not in seen


def test_the_standing_choice_also_covers_objective_steps_and_builds(isolated_home, monkeypatch):
    from waggle import jobs, objectives, toolkit
    comfort.set_value("job_model", "fable", owner())
    comfort.set_value("job_effort", "high", owner())
    made = {}
    monkeypatch.setattr(jobs, "create", lambda *a, **k: "job-1")
    monkeypatch.setattr(jobs, "update", lambda jid, **k: made.update(k))
    monkeypatch.setattr(jobs, "launch", lambda jid: None)
    monkeypatch.setattr(toolkit.quota, "can_escalate", lambda model, s: (True, ""))
    assert json.loads(toolkit.start_build({"title": "App", "prompt": "build it"}))["started"] and made == {"model": "fable"}
    monkeypatch.setattr(objectives.quota, "can_escalate", lambda m, s: (True, ""))
    monkeypatch.setattr(objectives, "kick", lambda oid: None)
    step = {"title": "Find them", "detail": "search", "agent": "researcher", "done_when": "3 listed", "depends_on": []}
    s = Settings.load()
    oid = objectives.create("Find three internships", asker=lambda *a, **k: {"json": {"title": "Hunt", "steps": [step]}},
                            settings=s)
    used = {}

    def run(agent_name, prompt, **k):
        used.update(k)
        return agents.runner.AgentResult(text="did it", ok=True)
    from waggle import agents
    objectives.run_step(oid, settings=s, run_fn=run, verify_fn=lambda *a, **k: (True, ""))
    assert used["model"] == "fable" and used["effort"] == "high"


def test_the_page_picker_changes_the_same_setting(settings):
    assert home.brain()["chat_model"] == "haiku" and home.brain()["job_model"] == ""
    out = home.set_brain({"chat_model": "sonnet", "job_model": "opus"})
    assert out["ok"] and out["chat_model"] == "sonnet" and out["job_model"] == "opus"
    assert home.set_brain({"chat_model": "nonsense"})["ok"] is False and home.brain()["chat_model"] == "sonnet"
    assert home.state()["brain"]["chat_model"] == "sonnet"


def test_a_running_chat_moves_to_the_newly_chosen_model_and_keeps_the_conversation(isolated_home):
    fake = FakeClaude()
    s = ChatSession("telegram:1", popen=fake, claude="claude")  # no injected settings: it follows config.json
    assert s.turn("one").ok and fake.procs[0].arg("--model") == "claude-haiku-5-5"
    comfort.set_value("chat_model", "opus", owner())
    res = s.turn("two")
    assert res.ok and len(fake.procs) == 2 and fake.procs[1].arg("--model") == "claude-opus-5-5"
    assert fake.procs[1].arg("--resume") == fake.procs[0].session_id
    s.turn("three")
    assert len(fake.procs) == 2  # no restart while the choice stays the same


def test_a_job_uses_the_model_the_owner_named_else_their_standing_choice(settings, monkeypatch):
    from waggle import agents
    used = []

    class Res:
        ok, text, error, approvals = True, "done", "", None
    monkeypatch.setattr(agents, "run", lambda name, task, **kw: used.append(kw.get("model")) or Res())
    monkeypatch.setenv("WAGGLE_CHAT", "telegram:1")
    out = json.loads(chat_jobs._t_background_task({"task": "research the zoning", "model": "Opus", "agent": "researcher"}))
    assert out["started"] and out["model"] == "opus" and out["agent"] == "researcher"
    assert "not a model" in chat_jobs._t_background_task({"task": "x", "model": "gpt-5"})
    base = {"chat": "telegram:1", "task": "t", "agent": "general", "hard": False, "origin": "user"}
    chat_jobs.run_background({**base, "key": "k1", "model": "opus"})
    chat_jobs.run_background({**base, "key": "k2"})
    comfort.set_value("job_model", "sonnet", owner())
    chat_jobs.run_background({**base, "key": "k3"})
    chat_jobs.run_background({**base, "key": "k4", "model": "fable"})
    assert used == ["opus", None, "sonnet", "fable"]


# ---- what it is doing, live ----

def test_the_live_feed_follows_one_turn_from_start_to_end(settings):
    assert liveturn.read() is None
    liveturn.begin("t1", "telegram:1", "check  my repos", "user", now=100.0)
    liveturn.tool({"tool": "Bash", "input": json.dumps({"command": "gh repo list --limit 5"})}, now=101.0)
    liveturn.tool({"tool": "mcp__waggle__vault_search", "input": '{"query": "zoning notes", "k": 3'}, now=102.0)  # cut JSON
    liveturn.words("Here is what I", now=103.0)
    st = liveturn.read(now=103.5)
    assert st["running"] and st["state"] == "writing" and st["asked"] == "check my repos"
    assert [s["line"] for s in st["steps"]][0] == "Bash: gh repo list --limit 5" and st["steps"][1]["line"].startswith("vault_search")
    liveturn.end(model="claude-sonnet-5-5", outcome="ok", reply="Here is what I found.", now=105.0)
    st = liveturn.read(now=106.0)
    assert not st["running"] and st["seconds"] == 5.0 and st["text"] == "Here is what I found." and st["state"] == "done"
    liveturn.tool({"tool": "Bash", "input": "{}"}, now=107.0)  # a late event after the end changes nothing
    assert len(liveturn.read(now=108.0)["steps"]) == 2


def test_a_turn_that_never_ended_stops_counting_as_running(settings):
    liveturn.begin("t2", "telegram:1", "x", now=100.0)
    assert liveturn.read(now=200.0)["running"] and not liveturn.read(now=100.0 + liveturn.STALE_S + 1)["running"]


def test_the_page_shows_the_owners_turn_and_running_jobs_but_never_test_turns(settings, monkeypatch):
    monkeypatch.setattr(home, "_owner_chat", lambda: "telegram:1")
    monkeypatch.setattr(chat_jobs, "status", lambda chat, limit=8, now=None: [
        {"job": 1, "task": "Build the site", "state": "running", "agent": "coder", "model": "opus",
         "progress": {"running_min": 3.5, "steps": 12, "recent_steps": ["Write: index.html"], "files_written": ["a"]}},
        {"job": 2, "task": "Old one", "state": "done"}])
    liveturn.begin("t3", "telegram:1", "hello", "user")
    liveturn.end(model="claude-opus-5-5", outcome="ok", reply="Hi.")
    out = home.live()
    assert out["turn"]["turn"] == "t3" and out["turn"]["model"] == "opus"
    assert [j["title"] for j in out["jobs"]] == ["Build the site"] and out["jobs"][0]["recent"] == ["Write: index.html"]
    liveturn.begin("t4", "telegram:1", "[waggle-test] ping", "canary")
    assert home.live()["turn"] is None


# ---- the conversation so far ----

def turn_row(text, answer, when, origin="user", turn_id="", extra=None, chat="telegram:1"):
    tid = traces.record(text, session=chat, route="chat", model="claude-sonnet-5-5", reply=answer, turn_id=turn_id,
                        chat=chat, origin=origin, answered_by="claude-sonnet-5-5", total_s=4.2, turn_outcome="ok",
                        extra=extra or {})
    con = traces._db()
    con.execute("UPDATE traces SET time=? WHERE id=?", (when, tid))
    con.commit()
    con.close()
    return tid


def test_history_is_the_owners_conversation_oldest_first_in_pages(settings, monkeypatch):
    monkeypatch.setattr(home, "_owner_chat", lambda: "telegram:1")
    for i in range(5):
        turn_row(f"question {i}", f"answer {i}", 1000.0 + i, turn_id=f"t{i}")
    turn_row("[waggle-test] ping", "pong", 1010.0, origin="canary")
    turn_row("someone else", "other chat", 1011.0, chat="telegram:2")
    turn_row("spoken one", "heard", 1012.0, turn_id="v1", extra={
        "voice": True, "tools": [{"tool": "Bash", "input": "{\"command\": \"dir\"}", "ok": True}],
        "voice_turn": {"transcript_source": "PC wake word"}})
    page = home.history(n=3)
    assert [t["request"] for t in page["turns"]] == ["question 3", "question 4", "spoken one"] and page["more"]
    last = page["turns"][-1]
    assert last["said"] == "spoken" and last["where"] == "pc voice" and last["tools"] == 1 and last["model"] == "sonnet"
    older = home.history(before=page["turns"][0]["time"], n=10)
    assert [t["request"] for t in older["turns"]] == ["question 0", "question 1", "question 2"] and not older["more"]
    one = home.turn("v1")
    assert one["reply"] == "heard" and one["steps"] == [{"line": "Bash: dir", "ok": True, "denied": False}]
    assert home.turn("nope") == {"error": "no such turn"}


def test_the_new_routes_are_served(settings, monkeypatch):
    import urllib.request
    from http.server import ThreadingHTTPServer

    from waggle import dashboard
    monkeypatch.setattr(dashboard, "health", lambda: {})
    monkeypatch.setattr(home, "_owner_chat", lambda: "telegram:1")
    monkeypatch.setattr(home, "hear", lambda body, ctype: {"ok": True, "text": f"{len(body)} bytes of {ctype}"})
    monkeypatch.setattr(home, "stop", lambda: {"ok": True, "stopped": False})
    monkeypatch.setattr(home, "say_aloud", lambda text, dictated=True: (200, "audio/wav", b"RIFFdata", "Hi.", "done",
                                                                        5.0, "turn9"))
    turn_row("hi", "hello", 1000.0, turn_id="t1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, ctype="application/json", token=home.TOKEN):
        req = urllib.request.Request(base + path, data=body, headers={"Content-Type": ctype, "X-Waggle-Token": token}
                                     if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
    try:
        assert json.loads(call("/api/history?n=5")[2])["turns"][0]["reply"] == "hello"
        assert json.loads(call("/api/turn?id=t1")[2])["request"] == "hi"
        assert "jobs" in json.loads(call("/api/live")[2])
        assert json.loads(call("/api/hear", b"abc", "audio/webm")[2])["text"] == "3 bytes of audio/webm"
        assert call("/api/hear", b"abc", "audio/webm", token="wrong")[0] == 403  # still only this page
        assert json.loads(call("/api/stop", b"{}")[2]) == {"ok": True, "stopped": False}
        code, headers, body = call("/api/say", json.dumps({"text": "hi", "speak": True, "dictated": True}).encode())
        assert code == 200 and body == b"RIFFdata" and headers["X-Waggle-Turn"] == "turn9"
        assert json.loads(call("/api/brain", json.dumps({"chat_model": "opus"}).encode())[2])["chat_model"] == "opus"
        page = call("/")[2].decode()
        for piece in ('id="mic"', 'id="hearing"', 'id="stop"', 'id="earlier"', 'id="brain"', 'id="activity"',
                      "/api/hear", "/api/history", "/api/live"):
            assert piece in page, piece
        assert "/api/talk" not in page  # the page never sends a recording straight into a turn any more
    finally:
        server.shutdown()
