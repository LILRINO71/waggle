"""Pure-logic checks of the Telegram test client (tools/testclient/tclib.py): no telethon, no network."""
import asyncio
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SRC = Path(__file__).resolve().parent.parent / "tools" / "testclient"
spec = importlib.util.spec_from_file_location("tclib_under_test", SRC / "tclib.py")
tclib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tclib)

CODE, PASSWORD, PHONE_RAW, HASH = "48151", "hunter2-secret", "+1 (555) 123-4567", "deadbeefcafe0123456789"


class NeedPassword(Exception):
    pass


class FakeClient:
    def __init__(self, authorized=False, needs_password=False, group=True, bot_member=True, replies=None):
        self.authorized, self.needs_password, self.group, self.bot_member = authorized, needs_password, group, bot_member
        self.replies, self.sent, self.log = replies or [], [], []

    async def connect(self):
        self.log.append("connect")

    async def disconnect(self):
        self.log.append("disconnect")

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.log.append("code")

    async def sign_in(self, phone=None, code=None, password=None):
        if password is None and self.needs_password:
            raise NeedPassword()
        self.authorized = True

    async def get_me(self):
        return NS(first_name="Ana")

    async def get_dialogs(self):
        return [NS(name="Family", id=1, is_group=True, is_channel=False),
                NS(name="Waggle Lab", id=-100123, is_group=True, is_channel=False, entity="ENT")] if self.group else []

    async def get_participants(self, entity):
        return [NS(bot=False, username="ana"), NS(bot=True, username="Waggle_Bot")] if self.bot_member else [NS(bot=False, username="ana")]

    async def get_entity(self, key):
        return NS(id=777 if isinstance(key, str) else key)

    async def send_message(self, group, text):
        self.sent.append(text)
        return NS(id=10)

    async def get_messages(self, group, limit, min_id):
        return self.replies


def run_login(client, tmp_path, typed_bot="", pw=PASSWORD):
    out = []
    answers = iter([PHONE_RAW, typed_bot])
    secrets = iter([CODE, pw])
    rc = asyncio.run(tclib.run_login(client, lambda p: next(answers), lambda p: next(secrets), out.append,
                                     NeedPassword, tmp_path))
    return rc, "\n".join(out)


def test_module_does_not_import_telethon():
    src = (SRC / "tclib.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+telethon", src, re.M)


def test_clean_username():
    assert tclib.clean_username(" @Waggle_Bot ") == "Waggle_Bot"
    assert tclib.clean_username("https://t.me/Waggle_Bot") == "Waggle_Bot"
    assert tclib.clean_username("ab") == ""
    assert tclib.clean_username("bad name!") == ""
    assert tclib.clean_username(None) == ""


def test_normalize_phone():
    assert tclib.normalize_phone(PHONE_RAW) == "+15551234567"
    assert tclib.normalize_phone("123") == ""
    assert tclib.normalize_phone("") == ""


def test_target_roundtrip_and_bad_files(tmp_path):
    assert tclib.load_target(tmp_path) is None
    tclib.save_target(-100123, "@Waggle_Bot", tmp_path)
    assert tclib.load_target(tmp_path) == {"group_id": -100123, "bot_username": "Waggle_Bot"}
    tclib.target_path(tmp_path).write_text("not json")
    assert tclib.load_target(tmp_path) is None
    tclib.target_path(tmp_path).write_text(json.dumps({"group_id": "x", "bot_username": "Waggle_Bot"}))
    assert tclib.load_target(tmp_path) is None
    tclib.target_path(tmp_path).write_text(json.dumps({"group_id": 5}))
    assert tclib.load_target(tmp_path) is None


def test_state_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WAGGLE_TESTCLIENT_DIR", str(tmp_path))
    assert tclib.state_dir() == tmp_path
    assert tclib.session_base().endswith("owner")  # telethon appends .session -> owner.session


def test_ping_text_format():
    t = tclib.new_token()
    assert re.fullmatch(r"[a-z0-9]{6}", t)
    assert tclib.ping_text("abc123") == "test-origin ping abc123: reply with the word pong"
    assert tclib.ping_text("abc123", "@Waggle_Bot") == "@Waggle_Bot test-origin ping abc123: reply with the word pong"


def test_short_reply_collapses_and_cuts():
    assert tclib.short_reply("a\n\nb   c") == "a b c"
    assert len(tclib.short_reply("x" * 500)) == 200


def test_pick_group_and_bot():
    ds = [NS(name="Waggle Lab", id=1, is_group=False, is_channel=False), NS(name="waggle lab", id=2, is_group=True)]
    assert tclib.pick_group(ds).id == 2
    assert tclib.pick_group([]) is None
    ms = [NS(bot=True, username="One_Bot"), NS(bot=False, username="x")]
    assert tclib.pick_bot(ms) == ("One_Bot", True)
    assert tclib.pick_bot(ms + [NS(bot=True, username="Two_Bot")]) == ("", False)  # ambiguous
    assert tclib.pick_bot(ms, "@two_bot") == ("two_bot", False)  # typed but not a member
    assert tclib.pick_bot(ms, "@one_bot") == ("One_Bot", True)


def test_scrub_and_leaks():
    assert tclib.scrub(f"bad {CODE} and {PASSWORD}", [CODE, PASSWORD]) == "bad [hidden] and [hidden]"
    assert tclib.leaks("clean text", [CODE]) == []
    assert tclib.leaks(f"x {CODE}", [CODE]) == [CODE]


def test_login_success_saves_target_and_leaks_nothing(tmp_path):
    rc, text = run_login(FakeClient(), tmp_path)
    assert rc == 0
    assert "Logged in as Ana" in text and "-100123" in text and "Waggle_Bot is a member" in text
    assert tclib.load_target(tmp_path) == {"group_id": -100123, "bot_username": "Waggle_Bot"}
    assert tclib.leaks(text, [CODE, PASSWORD, "15551234567", HASH]) == []


def test_login_with_2fa_leaks_nothing(tmp_path):
    client = FakeClient(needs_password=True)
    rc, text = run_login(client, tmp_path)
    assert rc == 0 and client.authorized
    assert tclib.leaks(text, [CODE, PASSWORD, "15551234567"]) == []


def test_login_error_message_is_scrubbed(tmp_path):
    class Boom(FakeClient):
        async def sign_in(self, phone=None, code=None, password=None):
            raise RuntimeError(f"bad code {code} for {phone}")
    rc, text = run_login(Boom(), tmp_path)
    assert rc == 1 and "RuntimeError" in text
    assert tclib.leaks(text, [CODE, "15551234567"]) == []


def test_login_without_group_or_bot(tmp_path):
    rc, text = run_login(FakeClient(group=False), tmp_path)
    assert rc == 2 and "Waggle Lab" in text and tclib.load_target(tmp_path) is None
    rc, text = run_login(FakeClient(bot_member=False), tmp_path, typed_bot="@Waggle_Bot")
    assert rc == 4 and "NOT a member" in text


def test_login_rejects_bad_phone(tmp_path):
    out = []
    rc = asyncio.run(tclib.run_login(FakeClient(), lambda p: "12", lambda p: "x", out.append, NeedPassword, tmp_path))
    assert rc == 1


def test_ping_reply_and_timeout():
    target = {"group_id": -100123, "bot_username": "Waggle_Bot"}
    now = [0.0]

    async def nap(s):
        now[0] += s

    reply = NS(sender_id=777, id=11, raw_text="pong! " + "y" * 400)
    out = []
    c = FakeClient(authorized=True, replies=[NS(sender_id=5, id=12, raw_text="someone else"), reply])
    assert asyncio.run(tclib.run_ping(c, target, out.append, token="abc123", clock=lambda: now[0], sleep=nap)) == 0
    assert c.sent == ["@Waggle_Bot test-origin ping abc123: reply with the word pong"]
    assert out[0].startswith("reply in 2.0s: pong!") and len(out[0].split(": ", 1)[1]) == 200

    now[0] = 0.0
    out = []
    c = FakeClient(authorized=True, replies=[NS(sender_id=5, id=12, raw_text="not the bot")])
    assert asyncio.run(tclib.run_ping(c, target, out.append, wait_s=6, token="t", clock=lambda: now[0], sleep=nap)) == 1
    assert "no reply" in out[0] and "disconnect" in c.log


def test_ping_requires_login():
    out = []
    c = FakeClient(authorized=False)
    assert asyncio.run(tclib.run_ping(c, {"group_id": 1, "bot_username": "Waggle_Bot"}, out.append)) == 1
    assert "login.py" in out[0] and c.sent == []


# ---- one user of the Telegram login at a time ------------------------------------------------------------------

def test_session_lock_is_exclusive_and_released(tmp_path):
    a, b = tclib.SessionLock(tmp_path), tclib.SessionLock(tmp_path)
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


def test_session_lock_waits_then_gives_up(tmp_path):
    holder = tclib.SessionLock(tmp_path)
    assert holder.try_acquire()
    t = [0.0]
    told = []
    waiter = tclib.SessionLock(tmp_path)
    ok = waiter.acquire(5, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s),
                        on_wait=lambda: told.append(1))
    assert ok is False and told == [1] and t[0] >= 5
    holder.release()
    assert waiter.acquire(5, clock=lambda: t[0], sleep=lambda s: None) is True
    waiter.release()


def test_session_lock_context_manager_releases(tmp_path):
    with tclib.SessionLock(tmp_path) as lock:
        assert lock.acquire(0)
    other = tclib.SessionLock(tmp_path)
    assert other.try_acquire()
    other.release()


@pytest.mark.parametrize("script", ["ask.py", "canary.py", "ping.py", "login.py"])
def test_every_script_takes_the_lock_before_opening_the_session(script):
    src = (SRC / script).read_text(encoding="utf-8")
    assert "SessionLock(" in src and "TelegramClient(tclib.session_base" in src
    assert src.index("SessionLock(") < src.index("TelegramClient(tclib.session_base")
