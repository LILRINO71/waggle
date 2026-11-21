"""A credential never leaves Waggle for the owner, whichever model wrote the reply: every channel redacts at its
lowest point (Telegram client, outbox, ntfy, speech, call, send_to_me, the stream writer, the engine's reply).
All secrets here are fake and built in code."""
import json
import threading
import time

import pytest
from engine_fakes import FakeClaude, FakeDelivery, ev_init, ev_result, ev_text

from waggle import chat_engine, notify, policy, voice_reply
from waggle.chat_session import ChatSession
from waggle.core import events, outbox, store
from waggle.core.telegram import BotApi
from waggle.delivery import CollectDelivery, StreamWriter

# ---- fake secrets (never real) -------------------------------------------------------------------------------

ANT = "sk-ant-" + "api03-" + "Ab1_" * 10
OPENAI = "sk-" + "Zq9" * 16
PROJ = "sk-proj-" + "Xy7_-" * 10
GHP = "ghp_" + "a1B2" * 9
GHPAT = "github_pat_" + "11ABCDEF0" * 3 + "_" + "q" * 40
TG = "123456789:" + "AAFk-" * 7
AWS = "AKIA" + "ABCDEFGH23456789"
GOOG = "AIza" + "SyD-" * 8 + "xyz"
SLACK = "xoxb-123456789012-abcdefghijkl"
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiJmYWtlIn0" + "." + "c2lnbmF0dXJlLWZha2U"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKfakefakefakefake\nZmFrZWZha2VmYWtlZmFrZQ==\n"
       "-----END RSA PRIVATE KEY-----")
SECRETS = {"ant": ANT, "openai": OPENAI, "proj": PROJ, "ghp": GHP, "ghpat": GHPAT, "tg": TG, "aws": AWS,
           "google": GOOG, "slack": SLACK, "jwt": JWT}


def clean_of_every_secret(text):
    text = str(text)
    return not any(s in text for s in list(SECRETS.values()) + ["hunter2xyz", "MIIBOgIBAAJB", "ZmFrZWZha2Vm"])


# ---- the patterns: what is caught ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SECRETS))
def test_common_credential_shapes_are_redacted(name):
    out = policy.redact(f"here you go: {SECRETS[name]} done")
    assert SECRETS[name] not in out and "[REDACTED:" in out and out.startswith("here you go: ")


def test_a_bot_token_inside_a_url_is_redacted():
    out = policy.redact(f"https://api.telegram.org/bot{TG}/getMe")
    assert TG not in out and "[REDACTED:telegram-token]" in out


def test_a_private_key_block_is_redacted_whole():
    out = policy.redact("key:\n" + PEM + "\nthanks")
    assert "MIIBOgIBAAJB" not in out and "BEGIN" not in out and out.endswith("thanks")


def test_a_private_key_block_still_being_written_is_redacted_to_the_end():
    # a streamed answer shows the header and part of the body before the END line has arrived
    out = policy.redact("key:\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU")
    assert "b3BlbnNz" not in out and "[REDACTED:private-key]" in out


@pytest.mark.parametrize("line", [
    "password: hunter2xyz", "Password = hunter2xyz", "PASSWD=hunter2xyz", '"password": "hunter2xyz"',
    "db_password: hunter2xyz", "aws_secret_access_key = hunter2xyz", "api_key=hunter2xyz", "my password is hunter2xyz1",
    "client_secret: hunter2xyz"])
def test_password_style_lines_are_redacted(line):
    out = policy.redact(line)
    assert "hunter2xyz" not in out and "[REDACTED:" in out


def test_redact_report_names_the_kinds_and_is_what_redact_returns():
    out, kinds = policy.redact_report(f"{ANT} and {GHP} and {ANT}")
    assert out == policy.redact(f"{ANT} and {GHP} and {ANT}")
    assert sorted(set(kinds)) == ["api-key", "github-token"] and kinds.count("api-key") == 2
    assert policy.redact_report("nothing here") == ("nothing here", [])


def test_redaction_is_idempotent():
    once = policy.redact(f"password: hunter2xyz {ANT} {PEM}")
    assert policy.redact(once) == once


# ---- the patterns: what must stay ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "commit 3f2a9c1b7d4e8a6f0b5c2d1e9a8b7c6d5e4f3a2b is the fix",
    "uuid 123e4567-e89b-12d3-a456-426614174000 stays",
    "Pneumonoultramicroscopicsilicovolcanoconiosisandotherlongwordsabcdefghijklmnop",
    "the risk-assessment-for-the-quarterly-review-of-the-robot is done",
    "a task-force-for-the-championship-qualifier-planning-session met",
    "Tokens: 1234567 used, max_tokens: 4096", "the password field is shown on the login page",
    "it is 12:34:56 now, 2026-11-21T10:30:00", "see https://example.com/some/long/path/with-dashes-and-words-in-it",
    "order 4111 1111 1111 1112 failed", "ref abcdef0123456789abcdef0123456789abcdef01", "sk-1 and ask-me later",
    "The Bearer of good news", "a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p_q_r_s_t_u_v_w_x_y_z", "call 701-555-0142 or 7015550142",
])
def test_normal_text_is_left_alone(text):
    assert policy.redact(text) == text


# ---- Telegram client: send, edit, caption ----------------------------------------------------------------

@pytest.fixture
def tg():
    """BotApi with its network call replaced by a recorder (no real Telegram, no socket)."""
    api = BotApi("123456:FAKE-TOKEN-not-real-0000000000000000")
    calls = []

    def call(method, params=None, *, files=None, read_timeout=None):
        calls.append((method, params, files))
        return {"message_id": len(calls)}
    api._call = call
    api.calls = calls
    return api


def _redacted_events():
    return [e for e in events.query("redacted_outbound")]


def test_send_message_redacts_every_part(tg):
    text = ("intro " + ANT + " ") + ("filler words " * 400) + ("tail " + GHP)
    tg.send_message("42", text)
    assert len(tg.calls) >= 2
    assert all(clean_of_every_secret(json.dumps(c[1])) for c in tg.calls)


def test_a_key_that_would_straddle_two_messages_is_gone_before_the_split(tg):
    filler = "x " * 2040 + ANT   # the key starts just before the 4096 mark
    tg.send_message("42", filler + " end")
    assert all(ANT[:12] not in json.dumps(c[1]) for c in tg.calls)


def test_edit_message_text_redacts(tg):
    tg.edit_message_text("42", 7, f"so the key is {ANT}")
    method, params, _ = tg.calls[0]
    assert method == "editMessageText" and ANT not in params["text"] and "[REDACTED:api-key]" in params["text"]


def test_captions_are_redacted(tg, tmp_path):
    f = tmp_path / "note.ogg"
    f.write_bytes(b"OggS")
    tg.send_voice("42", f, caption=f"say {ANT}")
    tg.send_document("42", f, caption=f"doc {GHP}")
    for method, _, files in tg.calls:
        assert clean_of_every_secret(files[1]["caption"]) and "[REDACTED:" in files[1]["caption"], method


def test_a_telegram_redaction_is_counted_without_the_secret(tg):
    tg.send_message("42", f"key {ANT} and {GHP}")
    ev = _redacted_events()
    assert len(ev) == 1 and ev[0]["data"]["channel"] == "telegram"
    assert sorted(ev[0]["data"]["kinds"]) == ["api-key", "github-token"]
    assert clean_of_every_secret(json.dumps(ev))


def test_clean_text_leaves_no_event_and_is_sent_unchanged(tg):
    tg.send_message("42", "robot parts are on the shelf")
    assert tg.calls[0][1]["text"] == "robot parts are on the shelf" and _redacted_events() == []


# ---- the outbox, ntfy, the call -----------------------------------------------------------------------------

def test_the_outbox_never_holds_or_hands_out_a_secret(settings):
    oid = outbox.enqueue("text", f"the key: {ANT}", chat="42")
    body = outbox.get(oid)["body"]
    assert ANT not in body and "[REDACTED:api-key]" in body
    ev = _redacted_events()
    assert ev and ev[-1]["data"]["channel"] == "outbox"


def test_a_push_never_carries_a_secret(settings):
    seen = []
    notify.push_sender({"id": 1, "body": f"alarm {GHP}", "meta": {"title": f"t {ANT}"}},
                       post=lambda url, body: seen.append(body) or {"id": "x1"})
    assert clean_of_every_secret(json.dumps(seen)) and "[REDACTED:" in seen[0]["message"]


def test_to_ntfy_never_carries_a_secret(settings):
    seen = []
    notify.to_ntfy(f"alarm {GHP}", title="t", post=lambda url, body: seen.append(body) or True)
    assert clean_of_every_secret(json.dumps(seen))


def test_a_call_never_speaks_a_secret():
    from waggle import call
    assert clean_of_every_secret(call.twiml(f"your key is {ANT}"))


def test_send_to_me_redacts_the_text(monkeypatch, tmp_path):
    import subprocess
    from waggle import waggle_mcp
    exe = tmp_path / "bin" / "hermes.exe"
    exe.parent.mkdir()
    exe.write_text("x")
    monkeypatch.setattr(notify, "hermes_exe", lambda: exe)
    got = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: got.append(cmd) or type("R", (), {"returncode": 0})())
    waggle_mcp.t_send_to_me({"text": f"the key {ANT}"})
    assert got and clean_of_every_secret(got[0][-1]) and "[REDACTED:api-key]" in got[0][-1]


# ---- speech -------------------------------------------------------------------------------------------------

def test_speakable_text_has_no_secret():
    said = voice_reply.speakable(f"Your key is {ANT} and the password: hunter2xyz")
    assert clean_of_every_secret(said)


def test_capped_spoken_text_has_no_secret():
    assert clean_of_every_secret(voice_reply.cap_spoken(f"Sure. The key is {GHP}. Anything else?"))


def test_streamed_spoken_sentences_have_no_secret():
    text = f"SPOKEN: The key is {ANT}. Sorry about that.\nANSWER:\nlater"
    sents = voice_reply.spoken_sentences(text)
    assert sents and clean_of_every_secret(" ".join(sents))


def test_the_speech_server_never_receives_a_secret(monkeypatch, tmp_path):
    seen = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": false, "error": "stop"}'
    monkeypatch.setattr(voice_reply.urllib.request, "urlopen", lambda req, timeout=0: seen.append(req.data) or R())
    with pytest.raises(RuntimeError):
        voice_reply.synth_wav(f"the key is {ANT}", tmp_path / "o.wav", "af_heart", 1.0, 5)
    assert seen and clean_of_every_secret(seen[0].decode())


def test_a_voice_note_is_never_made_from_a_secret(settings, tmp_path, monkeypatch):
    said = []
    notify.voice(f"the key is {ANT}", settings=settings, synth=lambda text, out, s=None: said.append(text) or False,
                 origin="t1")
    assert said and clean_of_every_secret(said[0])


# ---- the stream writer: every edit is redacted whole ---------------------------------------------------------

def test_every_streamed_edit_is_redacted_from_the_full_text():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.01, first_hold_s=0.0, first_min_chars=0, tick=0.005)
    full = "Sure. Here is the key " + ANT + " and the rest of the answer."
    for i in range(10, len(full) + 1, 7):
        w.update(full[:i])
        time.sleep(0.02)
    out = w.finish(full)
    assert out["delivered"]
    assert all(clean_of_every_secret(text) for _, _, text in d.log), d.log
    assert "[REDACTED:api-key]" in d.messages[0] and d.messages[0].endswith("the rest of the answer.")
    assert len([e for e in _redacted_events() if e["data"]["channel"] == "stream"]) == 1  # counted once, not per edit


def test_a_tool_status_line_is_redacted():
    d = FakeDelivery()
    w = StreamWriter(d, interval=0.01, tick=0.005)
    w._push_status(f"Running: curl -H 'Authorization: Bearer {'z' * 30}'")
    assert "z" * 30 not in d.messages[0]


def test_a_collect_delivery_reply_has_no_secret():
    d = CollectDelivery()
    StreamWriter(d, stream=False).finish(f"key {ANT}")
    assert clean_of_every_secret(d.text)


# ---- the engine: a model that pastes a secret ----------------------------------------------------------------

def _turn(settings, reply, delivery):
    def script(proc, text):
        sid = proc.session_id

        def go():
            proc.emit(ev_init(sid))
            for e in ev_text(sid, reply):
                proc.emit(e)
            proc.emit(ev_result(sid, reply))
        threading.Thread(target=go, daemon=True).start()
    settings.edit_interval_s = 0.05
    sess = ChatSession("telegram:5", settings=settings, popen=FakeClaude(script), claude="claude")
    return chat_engine.run_turn("telegram:5", "what is my key?", delivery, platform="telegram", context="",
                                settings=settings, session=sess)


@pytest.fixture(autouse=True)
def _close_store():
    from waggle import outredact
    outredact._recent.clear()   # the count throttle is per process: each test starts fresh
    yield
    store.close_all()


def test_a_reply_with_a_pasted_key_reaches_nobody_unredacted(settings):
    d = FakeDelivery()
    rec = _turn(settings, f"Your key is {ANT}, keep it safe.", d)
    assert clean_of_every_secret(rec.reply) and "[REDACTED:api-key]" in rec.reply
    assert d.messages and all(clean_of_every_secret(m) for m in d.messages)


def test_the_synchronous_answer_hermes_shows_has_no_secret(settings):
    d = CollectDelivery()
    rec = _turn(settings, f"password: hunter2xyz and {GHP}", d)
    assert clean_of_every_secret(rec.reply) and clean_of_every_secret(d.text)


def test_an_undelivered_reply_waits_in_the_outbox_without_the_secret(settings):
    d = FakeDelivery(fail_post=True)
    rec = _turn(settings, f"key {ANT}", d)
    assert rec.outbox_id and clean_of_every_secret(outbox.get(rec.outbox_id)["body"])
