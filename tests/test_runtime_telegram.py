"""Telegram client tests: a local fake Bot API on 127.0.0.1, a fake token, nothing real."""
import http.client
import json
import logging
import random
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from waggle.core import telegram
from waggle.core.telegram import BotApi, TelegramError

TOKEN = "123456:FAKE-TOKEN-not-real-0000000000000000"
CHAT = "424242"


class Fake:
    """Records every request; replies from `script` (one entry per request) or with a plain success."""

    def __init__(self):
        self.requests = []
        self.script = []
        self.next_id = 100
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with fake.lock:
            method = self.path.rsplit("/", 1)[-1]
            entry = {"path": self.path, "method": method, "headers": dict(self.headers), "body": body,
                     "port": self.client_address[1]}
            if self.headers.get("Content-Type", "").startswith("application/json"):
                entry["json"] = json.loads(body.decode("utf-8"))
            fake.requests.append(entry)
            step = fake.script.pop(0) if fake.script else None
            fake.next_id += 1
            mid = fake.next_id
        if step == "drop":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if isinstance(step, tuple) and step[0] == "sleep":
            import time
            time.sleep(step[1])
            step = None
        if step is None:
            result = {"getMe": {"id": 1, "is_bot": True, "username": "fake_bot"}, "getUpdates": []}.get(
                method, {"message_id": mid} if method.startswith("send") and method != "sendChatAction" else True)
            status, payload = 200, {"ok": True, "result": result}
        else:
            status, payload = step
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.fake = Fake()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    server.fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server.fake
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def no_real_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.setattr(telegram, "default_env_file", lambda: tmp_path / "no-such-hermes.env")
    monkeypatch.setattr(telegram, "_default", None)


@pytest.fixture
def api(fake):
    a = BotApi(TOKEN, base=fake.base, read_timeout=5.0)
    yield a
    a.close()


def err(code, description, **parameters):
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return (code, body)


def parse_multipart(request):
    ctype = request["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1].encode()
    chunks = request["body"].split(b"--" + boundary)
    assert chunks[0] == b"" and chunks[-1] == b"--\r\n"
    fields = {}
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n")
        head, _, content = chunk[2:-2].partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        name = lines[0].split('name="', 1)[1].split('"', 1)[0]
        filename = lines[0].split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in lines[0] else None
        ctype_line = lines[1].split(": ", 1)[1] if len(lines) > 1 else None
        fields[name] = (content, filename, ctype_line)
    return boundary, fields


# --- token handling -------------------------------------------------------------------------------------------

def test_token_is_read_at_first_use_not_at_construction(fake, tmp_path, monkeypatch):
    env = tmp_path / "hermes.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(telegram, "default_env_file", lambda: env)
    a = BotApi(base=fake.base)
    assert a._token is None
    a.get_me()
    assert f"/bot{TOKEN}/getMe" == fake.requests[0]["path"]
    a.close()


def test_missing_token_is_an_auth_error_without_any_request(fake):
    a = BotApi(base=fake.base)
    with pytest.raises(TelegramError) as e:
        a.get_me()
    assert e.value.kind == "auth" and e.value.permanent
    assert fake.requests == []


def test_load_token_and_home_chat_lookup(tmp_path, monkeypatch):
    env = tmp_path / "x.env"
    env.write_text("# comment\n\nexport TELEGRAM_BOT_TOKEN='quoted-fake-token'\n"
                   'TELEGRAM_HOME_CHANNEL="777"  \nOTHER=1\nbroken line\n', encoding="utf-8")
    assert telegram.load_token(env) == "quoted-fake-token"
    assert telegram.home_chat(env) == "777"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-process-env")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "888")
    assert telegram.load_token(env) == "from-process-env"
    assert telegram.home_chat(env) == "888"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL")
    env.write_text("TELEGRAM_BOT_TOKEN=plain # trailing\n", encoding="utf-8")
    assert telegram.load_token(env) == "plain"
    assert telegram.home_chat(env) is None
    with pytest.raises(TelegramError) as e:
        telegram.load_token(tmp_path / "missing.env")
    assert e.value.kind == "auth"


def test_redact():
    assert TOKEN not in telegram.redact(f"GET /bot{TOKEN}/getMe failed", TOKEN)
    assert "123456:" not in telegram.redact("bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 x", None)
    assert telegram.redact(None, TOKEN) == ""


def test_the_token_never_leaks_on_any_error_path(fake, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    cases = [
        err(401, "Unauthorized"), err(404, "Not Found"), err(403, "Forbidden: bot was blocked by the user"),
        err(400, "Bad Request: chat not found"), err(409, "Conflict: terminated by other getUpdates request"),
        err(429, "Too Many Requests", retry_after=3), err(500, "Internal"), err(502, "Bad Gateway"),
        (200, b"not json"), (503, b"<html>down</html>"),
        err(400, f"echo of the url /bot{TOKEN}/sendMessage"),
        "drop", "drop",
    ]
    api = BotApi(TOKEN, base=fake.base, read_timeout=2.0)
    seen = []
    for step in cases:
        fake.script = [step] if step != "drop" else ["drop", "drop"]
        try:
            api.send_message(CHAT, "hello")
        except TelegramError as e:
            seen.append(e)
    api.close()

    fake.script = [("sleep", 1.0)]
    slow = BotApi(TOKEN, base=fake.base, read_timeout=0.2)
    with pytest.raises(TelegramError) as e:
        slow.get_me()
    seen.append(e.value)
    slow.close()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    dead = BotApi(TOKEN, base=f"http://127.0.0.1:{dead_port}")
    with pytest.raises(TelegramError) as e:
        dead.get_me()
    seen.append(e.value)

    bad = BotApi(TOKEN + " with space", base=fake.base)
    with pytest.raises(TelegramError) as e:
        bad.get_me()
    seen.append(e.value)

    real_request = http.client.HTTPConnection.request

    def leaky(self, method, url, *a, **k):
        raise OSError(f"cannot reach http://api.example.invalid{url}")
    monkeypatch.setattr(http.client.HTTPConnection, "request", leaky)
    leaky_api = BotApi(TOKEN, base=fake.base)
    with pytest.raises(TelegramError) as e:
        leaky_api.get_me()
    seen.append(e.value)
    monkeypatch.setattr(http.client.HTTPConnection, "request", real_request)

    assert len(seen) == 17
    for e in seen:
        assert e.__context__ is None and e.__cause__ is None
        for text in (str(e), repr(e), e.description, "".join(traceback.format_exception(e))):
            assert TOKEN not in text and "FAKE-TOKEN" not in text
    for a in (api, slow, dead, bad, leaky_api):
        assert TOKEN not in repr(a) and "FAKE-TOKEN" not in repr(a)
    assert caplog.records
    assert TOKEN not in caplog.text and "FAKE-TOKEN" not in caplog.text


# --- error mapping --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("step,kind,permanent", [
    (err(500, "boom"), "retryable", False), (err(502, "bad gateway"), "retryable", False),
    (err(401, "Unauthorized"), "auth", True), (err(404, "Not Found"), "auth", True),
    (err(403, "Forbidden: bot was blocked by the user"), "permanent", True),
    (err(400, "Bad Request: chat not found"), "permanent", True),
    (err(409, "Conflict"), "conflict", False), (err(429, "Too Many Requests", retry_after=11), "rate_limited", False),
])
def test_status_maps_to_kind(api, fake, step, kind, permanent):
    fake.script = [step]
    with pytest.raises(TelegramError) as e:
        api.send_message(CHAT, "hi")
    assert e.value.kind == kind and e.value.permanent is permanent
    assert e.value.status == step[0]
    assert len(fake.requests) == 1


def test_rate_limit_carries_retry_after(api, fake):
    fake.script = [err(429, "Too Many Requests: retry after 11", retry_after=11)]
    with pytest.raises(TelegramError) as e:
        api.get_me()
    assert e.value.retry_after == 11 and e.value.kind == "rate_limited"
    fake.script = [err(500, "x")]
    with pytest.raises(TelegramError) as e:
        api.get_me()
    assert e.value.retry_after is None


def test_network_errors_are_retryable(fake):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TelegramError) as e:
        BotApi(TOKEN, base=f"http://127.0.0.1:{port}").get_me()
    assert e.value.kind == "retryable" and not e.value.permanent


def test_timeout_is_retryable_and_not_resent(fake):
    fake.script = [("sleep", 1.0)]
    a = BotApi(TOKEN, base=fake.base, read_timeout=0.2)
    with pytest.raises(TelegramError) as e:
        a.send_message(CHAT, "hi")
    assert e.value.kind == "retryable"
    assert len(fake.requests) == 1
    a.close()


def test_garbage_body_is_retryable(api, fake):
    fake.script = [(200, b"<html>captive portal</html>")]
    with pytest.raises(TelegramError) as e:
        api.get_me()
    assert e.value.kind == "retryable"


def test_edit_not_modified_is_success(api, fake):
    fake.script = [err(400, "Bad Request: message is not modified: specified new message content and reply "
                            "markup are exactly the same as a current content")]
    assert api.edit_message_text(CHAT, 55, "same") is True
    fake.script = [err(400, "Bad Request: message to edit not found")]
    with pytest.raises(TelegramError) as e:
        api.edit_message_text(CHAT, 55, "gone")
    assert e.value.kind == "permanent"


def test_edit_sends_the_right_payload(api, fake):
    assert api.edit_message_text(CHAT, "55", "new text", parse_mode="HTML") is True
    assert fake.requests[0]["method"] == "editMessageText"
    assert fake.requests[0]["json"] == {"chat_id": 424242, "message_id": 55, "text": "new text", "parse_mode": "HTML"}


def test_parse_failure_retries_once_without_parse_mode(api, fake):
    fake.script = [err(400, "Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 5")]
    assert api.send_message(CHAT, "*oops", parse_mode="MarkdownV2") == [102]
    assert len(fake.requests) == 2
    assert fake.requests[0]["json"]["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in fake.requests[1]["json"]
    assert fake.requests[1]["json"]["text"] == "*oops"


def test_parse_failure_retry_happens_only_once(api, fake):
    fake.script = [err(400, "Bad Request: can't parse entities: x"), err(400, "Bad Request: can't parse entities: y")]
    with pytest.raises(TelegramError):
        api.send_message(CHAT, "*oops", parse_mode="MarkdownV2")
    assert len(fake.requests) == 2


def test_other_400_does_not_retry_and_no_parse_mode_means_no_retry(api, fake):
    fake.script = [err(400, "Bad Request: chat not found")]
    with pytest.raises(TelegramError):
        api.send_message(CHAT, "hi", parse_mode="HTML")
    assert len(fake.requests) == 1
    fake.script = [err(400, "Bad Request: can't parse entities")]
    with pytest.raises(TelegramError):
        api.send_message(CHAT, "hi")
    assert len(fake.requests) == 2


# --- the message calls ----------------------------------------------------------------------------------------

def test_basic_calls(api, fake):
    assert api.get_me()["username"] == "fake_bot"
    assert api.send_chat_action(CHAT) is True
    assert api.send_chat_action(CHAT, "record_voice") is True
    assert api.send_message(CHAT, "hello", reply_to=9) == [104]
    send = fake.requests[-1]["json"]
    assert send["chat_id"] == 424242 and send["text"] == "hello"
    assert send["reply_parameters"]["message_id"] == 9
    assert send["link_preview_options"] == {"is_disabled": True}
    assert fake.requests[1]["json"] == {"chat_id": 424242, "action": "typing"}
    assert fake.requests[2]["json"]["action"] == "record_voice"
    api.send_message("@channelname", "x", disable_preview=False)
    last = fake.requests[-1]["json"]
    assert last["chat_id"] == "@channelname" and "link_preview_options" not in last


def test_get_updates_passes_offset_and_widens_the_read_timeout(api, fake):
    fake.script = [(200, {"ok": True, "result": [{"update_id": 5}]})]
    assert api.get_updates(offset=5, timeout=40, limit=10) == [{"update_id": 5}]
    assert fake.requests[0]["json"] == {"timeout": 40, "limit": 10, "offset": 5}
    assert api._local.conn.sock.gettimeout() == 50
    api.get_updates()
    assert "offset" not in fake.requests[1]["json"]


def test_long_text_goes_out_in_parts_and_every_id_comes_back(api, fake):
    paragraphs = [f"paragraph {i} " + "word " * 300 for i in range(12)]
    text = "\n\n".join(paragraphs)
    assert len(text) > 3 * 4096
    ids = api.send_message(CHAT, text, reply_to=3)
    sent = [r["json"] for r in fake.requests]
    assert len(ids) == len(sent) >= 4 and len(set(ids)) == len(ids)
    assert all(0 < len(s["text"]) <= 4096 for s in sent)
    assert "".join(s["text"] for s in sent) == text
    assert "reply_parameters" in sent[0] and all("reply_parameters" not in s for s in sent[1:])


def test_partial_failure_reports_what_was_sent(api, fake):
    fake.script = [None, err(500, "boom")]
    with pytest.raises(TelegramError) as e:
        api.send_message(CHAT, "a" * 5000)
    assert e.value.sent == [101]


def test_empty_text_is_permanent(api, fake):
    with pytest.raises(TelegramError) as e:
        api.send_message(CHAT, "  \n ")
    assert e.value.kind == "permanent" and fake.requests == []


# --- split_text -----------------------------------------------------------------------------------------------

def test_split_prefers_paragraph_then_line_then_word():
    a, b = "a" * 60, "b" * 60
    assert telegram.split_text(f"{a}\n\n{b}", limit=100) == [f"{a}\n\n", b]
    assert telegram.split_text(f"{a}\n{b}", limit=100) == [f"{a}\n", b]
    assert telegram.split_text(f"{a} {b}", limit=100) == [f"{a} ", b]
    assert telegram.split_text("c" * 250, limit=100) == ["c" * 100, "c" * 100, "c" * 50]
    assert telegram.split_text("") == [] and telegram.split_text(" \n") == []
    assert telegram.split_text("short") == ["short"]


def test_split_picks_the_highest_level_available():
    text = "x" * 40 + " " + "y" * 20 + "\n" + "z" * 30 + "\n\n" + "w" * 20
    parts = telegram.split_text(text, limit=100)
    assert parts[0].endswith("\n\n") and len(parts) == 2


def test_split_never_loses_text_or_exceeds_the_limit():
    rng = random.Random(7)
    alphabet = "ab \n\n  é😀"
    for _ in range(300):
        limit = rng.choice([1, 2, 5, 17, 40, 100])
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 600))).rstrip() + "z"
        parts = telegram.split_text(text, limit=limit)
        assert all(p and p.strip() and len(p) <= limit for p in parts)
        assert "".join("".join(parts).split()) == "".join(text.split())
        if not any(c.isspace() for c in text):
            assert "".join(parts) == text


# --- files ----------------------------------------------------------------------------------------------------

def test_voice_goes_out_as_hand_built_multipart(api, fake, tmp_path):
    payload = bytes(range(256)) + b"\r\n--not-a-boundary\r\n\x00\xff\r\n"
    voice = tmp_path / "note ünï.ogg"
    voice.write_bytes(payload)
    assert api.send_voice(CHAT, voice, caption="listen", duration=7, reply_to=12) == 101
    req = fake.requests[0]
    assert req["method"] == "sendVoice"
    assert int(req["headers"]["Content-Length"]) == len(req["body"])
    boundary, fields = parse_multipart(req)
    assert set(fields) == {"chat_id", "voice", "caption", "duration", "reply_parameters"}
    assert fields["voice"][0] == payload
    assert fields["voice"][1] == "note ünï.ogg" and fields["voice"][2] == "audio/ogg"
    assert fields["chat_id"][0] == b"424242" and fields["caption"][0] == b"listen"
    assert fields["duration"][0] == b"7"
    assert json.loads(fields["reply_parameters"][0])["message_id"] == 12
    api.send_voice(CHAT, voice)
    boundary2, fields2 = parse_multipart(fake.requests[1])
    assert boundary2 != boundary and set(fields2) == {"chat_id", "voice"}


def test_document_goes_out_as_multipart(api, fake, tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4\r\nbinary\x00\x01\r\n")
    assert api.send_document(CHAT, doc, caption="here") == 101
    _b, fields = parse_multipart(fake.requests[0])
    assert fake.requests[0]["method"] == "sendDocument"
    assert set(fields) == {"chat_id", "document", "caption"}
    assert fields["document"] == (b"%PDF-1.4\r\nbinary\x00\x01\r\n", "report.pdf", "application/pdf")


def test_unreadable_file_is_permanent(api, fake, tmp_path):
    with pytest.raises(TelegramError) as e:
        api.send_document(CHAT, tmp_path / "nope.bin")
    assert e.value.kind == "permanent" and fake.requests == []


# --- connection -----------------------------------------------------------------------------------------------

def test_one_keepalive_connection_is_reused(api, fake):
    for _ in range(3):
        api.get_me()
    assert len({r["port"] for r in fake.requests}) == 1


def test_each_thread_gets_its_own_connection(api, fake):
    api.get_me()
    t = threading.Thread(target=api.get_me)
    t.start()
    t.join()
    assert len({r["port"] for r in fake.requests}) == 2


def test_dropped_keepalive_connection_reconnects_and_retries_once(api, fake):
    api.get_me()
    fake.script = ["drop"]
    api.get_me()
    assert len(fake.requests) == 3
    assert fake.requests[1]["port"] == fake.requests[0]["port"]
    assert fake.requests[2]["port"] != fake.requests[0]["port"]


def test_a_second_drop_is_an_error_not_a_loop(api, fake):
    api.get_me()
    fake.script = ["drop", "drop"]
    with pytest.raises(TelegramError) as e:
        api.get_me()
    assert e.value.kind == "retryable"
    assert len(fake.requests) == 3


def test_ipv4_is_resolved_first_then_ipv6(fake, monkeypatch):
    families = []
    real = socket.getaddrinfo

    def spy(host, port, family=0, *a, **k):
        families.append(family)
        return real(host, port, family, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", spy)
    BotApi(TOKEN, base=fake.base).get_me()
    assert families[0] == socket.AF_INET and socket.AF_INET6 not in families

    families.clear()

    def v4_missing(host, port, family=0, *a, **k):
        families.append(family)
        if family == socket.AF_INET:
            raise socket.gaierror("no A record")
        return real(host, port, socket.AF_INET, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", v4_missing)
    BotApi(TOKEN, base=fake.base).get_me()
    assert families == [socket.AF_INET, socket.AF_INET6]


def test_timeouts_default_and_read_timeout_is_applied(api, fake):
    d = BotApi(TOKEN)
    assert d.connect_timeout == 3.0 and d.read_timeout == 30.0
    api.get_me()
    assert api._local.conn.sock.gettimeout() == 5.0
    assert api._local.conn.timeout == 3.0


def test_repr_has_no_token(api):
    api.get_me()
    assert TOKEN not in repr(api) and "FAKE" not in repr(api)


# --- send_row -------------------------------------------------------------------------------------------------

def row(**kw):
    base = {"id": 1, "kind": "text", "chat": CHAT, "body": "hello", "file": None, "meta": {}}
    base.update(kw)
    return base


def test_text_row_returns_the_last_part_id_as_a_string(api, fake):
    assert telegram.send_row(row(body="a" * 9000), api) == "103"
    assert len(fake.requests) == 3


def test_text_row_uses_meta(api, fake):
    telegram.send_row(row(meta={"parse_mode": "HTML", "reply_to": 8}), api)
    sent = fake.requests[0]["json"]
    assert sent["parse_mode"] == "HTML" and sent["reply_parameters"]["message_id"] == 8


def test_row_without_chat_falls_back_to_the_home_chat(api, fake, monkeypatch):
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "999")
    telegram.send_row(row(chat=""), api)
    assert fake.requests[0]["json"]["chat_id"] == 999


def test_row_without_any_chat_is_permanent(api, fake):
    with pytest.raises(TelegramError) as e:
        telegram.send_row(row(chat=""), api)
    assert e.value.kind == "permanent" and e.value.permanent and fake.requests == []


@pytest.mark.parametrize("kind", ["push", "call", "mystery"])
def test_other_kinds_have_no_transport(api, fake, kind):
    with pytest.raises(TelegramError) as e:
        telegram.send_row(row(kind=kind), api)
    assert e.value.kind == "permanent" and "no Telegram transport for kind" in str(e.value)
    assert fake.requests == []


def test_voice_and_file_rows_send_the_file_with_the_body_as_caption(api, fake, tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"OggS-fake")
    assert telegram.send_row(row(kind="voice", file=str(f), body="cap", meta={"reply_to": 4}), api) == "101"
    _b, fields = parse_multipart(fake.requests[0])
    assert fields["voice"][0] == b"OggS-fake" and fields["caption"][0] == b"cap"
    d = tmp_path / "b.txt"
    d.write_text("data")
    assert telegram.send_row(row(kind="file", file=str(d), body=""), api) == "102"
    _b, fields = parse_multipart(fake.requests[1])
    assert fields["document"][0] == b"data" and "caption" not in fields


def test_missing_file_falls_back_to_text_only_when_allowed(api, fake, tmp_path):
    gone = str(tmp_path / "gone.ogg")
    assert telegram.send_row(row(kind="voice", file=gone, body="say this", meta={"text_fallback": True}), api) == "101"
    assert fake.requests[0]["method"] == "sendMessage" and fake.requests[0]["json"]["text"] == "say this"
    with pytest.raises(TelegramError) as e:
        telegram.send_row(row(kind="voice", file=gone, body="say this"), api)
    assert e.value.kind == "permanent" and len(fake.requests) == 1
    with pytest.raises(TelegramError):
        telegram.send_row(row(kind="file", file=None, body="x", meta={}), api)


def test_long_body_with_a_file_goes_as_a_following_message(api, fake, tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"x")
    assert telegram.send_row(row(kind="voice", file=str(f), body="t" * 2000), api) == "101"
    assert [r["method"] for r in fake.requests] == ["sendVoice", "sendMessage"]
    _b, fields = parse_multipart(fake.requests[0])
    assert "caption" not in fields


def test_default_api_is_shared_and_makes_no_request(monkeypatch):
    called = []
    monkeypatch.setattr(telegram, "load_token", lambda env_file=None: called.append(1) or TOKEN)
    a = telegram.default_api()
    assert a is telegram.default_api() and called == []
    assert a.base == "https://api.telegram.org"


def test_importing_the_module_reads_no_token(monkeypatch):
    monkeypatch.setattr(telegram, "load_token", lambda env_file=None: pytest.fail("read at import"))
    import importlib
    importlib.reload(telegram)
