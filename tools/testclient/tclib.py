"""Pure logic of the Telegram test client (login + ping). Never imports telethon, so the main suite can import it.

The scripts login.py and ping.py run with the test venv's python (telethon lives only there) and pass the real
client in; the tests pass a fake one. Every line that reaches `out` is scrubbed of the secrets the run knows."""
import json
import os
import random
import string
import time
from pathlib import Path

GROUP_NAME = "Waggle Lab"
PING_WAIT_S = 90
POLL_S = 2.0
REPLY_CHARS = 200
SESSION_NAME = "owner"


def state_dir():
    return Path(os.environ.get("WAGGLE_TESTCLIENT_DIR") or (Path.home() / ".waggle-testclient"))


def session_base(d=None):
    """Telethon adds ".session" itself: this is the path without it."""
    return str((d or state_dir()) / SESSION_NAME)


def api_path(d=None):
    return (d or state_dir()) / "api.json"


def target_path(d=None):
    return (d or state_dir()) / "target.json"


def restrict(path):
    """Best effort: only the current user may use the folder (icacls on Windows). Returns True when it ran."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
            return True
        except OSError:
            return False
    import subprocess
    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                           capture_output=True, timeout=20, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return r.returncode == 0
    except Exception:
        return False


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clean_username(raw):
    """"@Waggle_Bot " -> "Waggle_Bot"; "" when it is not a plausible Telegram username."""
    u = (raw or "").strip()
    if u.lower().startswith("https://t.me/"):
        u = u[len("https://t.me/"):]
    u = u.lstrip("@").strip()
    ok = 4 <= len(u) <= 32 and all(c.isalnum() or c == "_" for c in u)
    return u if ok else ""


def save_target(group_id, bot_username, d=None):
    save_json(target_path(d), {"group_id": int(group_id), "bot_username": clean_username(bot_username)})


def load_target(d=None):
    """{"group_id": int, "bot_username": str} or None when missing or incomplete."""
    t = load_json(target_path(d))
    if not t:
        return None
    try:
        gid = int(t.get("group_id"))
    except (TypeError, ValueError):
        return None
    bot = clean_username(t.get("bot_username"))
    return {"group_id": gid, "bot_username": bot} if bot else None


def normalize_phone(raw):
    """International format: "+" and digits only, "" when it does not look like one."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return "+" + digits if 7 <= len(digits) <= 15 else ""


def new_token(n=6):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def ping_text(token, bot_username=""):
    base = f"test-origin ping {token}: reply with the word pong"
    name = clean_username(bot_username)
    return f"@{name} {base}" if name else base


def short_reply(text, n=REPLY_CHARS):
    return " ".join((text or "").split())[:n]


def scrub(text, secrets):
    out = str(text)
    for s in secrets:
        if s and len(str(s)) >= 4:
            out = out.replace(str(s), "[hidden]")
    return out


def leaks(output, secrets):
    """The secrets (length >= 4) that appear in the output. Empty list means clean."""
    return [s for s in secrets if s and len(str(s)) >= 4 and str(s) in output]


def pick_group(dialogs, name=GROUP_NAME):
    """The dialog named exactly `name` (case-insensitive) that is a group or channel, else None."""
    for d in dialogs:
        if (getattr(d, "name", "") or "").strip().lower() == name.lower() and (
                getattr(d, "is_group", False) or getattr(d, "is_channel", False)):
            return d
    return None


def pick_bot(members, typed=""):
    """(username, is_member): the typed username when given, else the one bot member of the group."""
    t = clean_username(typed)
    bots = [m for m in members if getattr(m, "bot", False) and getattr(m, "username", None)]
    if t:
        for b in bots:
            if b.username.lower() == t.lower():
                return b.username, True
        return t, False
    if len(bots) == 1:
        return bots[0].username, True
    return "", False


CODE_ROUTES = {"SentCodeTypeApp": "as a message in your Telegram app (the chat named Telegram, blue check)",
               "SentCodeTypeSms": "by SMS text message", "SentCodeTypeCall": "by a phone call",
               "SentCodeTypeFlashCall": "by a missed call (the code is the last digits of the number)",
               "SentCodeTypeMissedCall": "by a missed call (the code is the last digits of the number)",
               "SentCodeTypeEmailCode": "to your Telegram login email",
               "SentCodeTypeFragmentSms": "through Fragment (fragment.com)",
               "SentCodeTypeFirebaseSms": "by SMS text message",
               "SentCodeTypeSetUpEmailRequired": "nowhere yet: Telegram wants a login email set up in the app first"}


def code_route(sent):
    """Where Telegram says it sent the login code (no secret in it)."""
    kind = type(getattr(sent, "type", None)).__name__
    return CODE_ROUTES.get(kind, f"by an unknown route ({kind})")


async def qr_authorize(client, ask_secret, out, password_needed_exc, show_qr, secrets, tries=4, wait_s=60):
    """Log in by scanning a QR code with the Telegram app (Settings > Devices > Link Desktop Device): no login code.
    The QR token is a secret while it is valid, so it is only drawn, never printed as text."""
    qr = await client.qr_login()
    for attempt in range(tries):
        show_qr(qr.url)
        out("Scan it with your phone: Telegram > Settings > Devices > Link Desktop Device. Waiting 60 s...")
        try:
            await qr.wait(timeout=wait_s)
            return
        except password_needed_exc:
            pw = ask_secret("2FA password: ")
            secrets.append(pw)
            await client.sign_in(password=pw)
            return
        except Exception as e:  # asyncio.TimeoutError: the token expired, draw a fresh one
            if "Timeout" not in type(e).__name__:
                raise
            out("Not scanned in time; here is a fresh code.")
            await qr.recreate()
    raise RuntimeError("the QR code was not scanned")


async def run_login(client, ask, ask_secret, out, password_needed_exc, d=None, show_qr=None):
    """Interactive login on an already built client. Returns 0 on success.
    ask / ask_secret / out are injected so a test can drive it with a fake client."""
    phone = None
    if show_qr is None:
        phone = normalize_phone(ask("Phone number (international format, e.g. +15551234567): "))
        if not phone:
            out("That does not look like an international phone number.")
            return 1
    typed_bot = ask("Waggle bot @username (Enter to auto-detect from the group): ")
    secrets = [phone] if phone else []
    await client.connect()
    try:
        if not await client.is_user_authorized() and show_qr is not None:
            await qr_authorize(client, ask_secret, out, password_needed_exc, show_qr, secrets)
        elif not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            out(f"Telegram sent the code {code_route(sent)}.")
            code = ask_secret("Login code (press Enter alone if nothing came, to have it sent another way): ").strip()
            if not code:
                sent = await client.send_code_request(phone, force_sms=True)
                out(f"Sent again, {code_route(sent)}.")
                code = ask_secret("Login code: ").strip()
            secrets.append(code)
            try:
                await client.sign_in(phone, code)
            except password_needed_exc:
                pw = ask_secret("2FA password: ")
                secrets.append(pw)
                await client.sign_in(password=pw)
        me = await client.get_me()
        out(f"Logged in as {scrub(getattr(me, 'first_name', '') or 'unknown', secrets)}")
        group = pick_group(await client.get_dialogs())
        if group is None:
            out(f'No group named "{GROUP_NAME}" among your chats. Create it, add the bot, run login again.')
            return 2
        out(f'Group "{GROUP_NAME}" id: {group.id}')
        bot_name, is_member = pick_bot(await client.get_participants(group.entity), typed_bot)
        if not bot_name:
            out("Could not tell which bot to test: type its @username when asked, and add it to the group.")
            return 3
        out(f"Bot @{bot_name} is {'a member' if is_member else 'NOT a member'} of the group.")
        save_target(group.id, bot_name, d)
        return 0 if is_member else 4
    except Exception as e:
        out(f"Login failed: {type(e).__name__}: {scrub(e, secrets)}")
        return 1
    finally:
        await client.disconnect()


async def run_ping(client, target, out, wait_s=PING_WAIT_S, poll_s=POLL_S, token=None, clock=time.monotonic,
                   sleep=None):
    """Send the ping, wait for a bot reply. Returns 0 (reply) or 1 (none, or an error)."""
    import asyncio
    sleep = sleep or asyncio.sleep
    token = token or new_token()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            out("Not logged in: run login.py first.")
            return 1
        try:
            group = await client.get_entity(target["group_id"])
        except ValueError:
            await client.get_dialogs()
            group = await client.get_entity(target["group_id"])
        bot = await client.get_entity(target["bot_username"])
        sent = await client.send_message(group, ping_text(token, target["bot_username"]))
        t0 = clock()
        while clock() - t0 < wait_s:
            await sleep(poll_s)
            for m in await client.get_messages(group, limit=20, min_id=sent.id):
                if getattr(m, "sender_id", None) == bot.id and m.id > sent.id and (m.raw_text or "").strip():
                    out(f"reply in {clock() - t0:.1f}s: {short_reply(m.raw_text)}")
                    return 0
        out(f"no reply from @{target['bot_username']} within {wait_s}s")
        return 1
    except Exception as e:
        out(f"ping failed: {type(e).__name__}: {e}")
        return 1
    finally:
        await client.disconnect()


# ---- one user of the Telegram login at a time --------------------------------------------------------------------
# ask.py, canary.py, ping.py and login.py all open the same Telethon session (owner.session, a SQLite file). Two at
# once fail with "database is locked", which once made a deploy's live check fail and roll back a good change. Each
# script holds this lock (a byte-range lock, released by the OS if the script dies) for as long as it is connected.

SESSION_WAIT_S = 180
LOCK_NAME = "session.use.lock"


class SessionLock:
    def __init__(self, d=None):
        self.path = (d or state_dir()) / LOCK_NAME
        self._f = None

    def try_acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def acquire(self, wait_s=SESSION_WAIT_S, *, clock=time.monotonic, sleep=time.sleep, poll_s=1.0, on_wait=None):
        """True once held; False when another run still holds it after wait_s seconds."""
        t0 = clock()
        told = False
        while not self.try_acquire():
            if clock() - t0 >= wait_s:
                return False
            if on_wait and not told:
                on_wait()
                told = True
            sleep(poll_s)
        return True

    def release(self):
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
