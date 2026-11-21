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


async def run_login(client, ask, ask_secret, out, password_needed_exc, d=None):
    """Interactive login on an already built client. Returns 0 on success.
    ask / ask_secret / out are injected so a test can drive it with a fake client."""
    phone = normalize_phone(ask("Phone number (international format, e.g. +15551234567): "))
    if not phone:
        out("That does not look like an international phone number.")
        return 1
    typed_bot = ask("Waggle bot @username (Enter to auto-detect from the group): ")
    secrets = [phone]
    await client.connect()
    try:
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = ask_secret("Login code Telegram sent you: ").strip()
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
