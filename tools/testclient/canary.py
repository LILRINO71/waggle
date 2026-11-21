r"""Waggle's end-to-end check ("canary"), run with the test venv's python by waggle.canary (the daily job):
    %USERPROFILE%\.waggle-testclient\venv\Scripts\python.exe tools\testclient\canary.py [--no-reminder]
It talks to the bot as the owner, like ask.py, with the "[waggle-test]" mark (Waggle answers normally and records
origin "canary", so nothing learns from it):
  1. roundtrip  "canary <id>: reply with exactly: pong <id>"  -> waits up to 90 s for a bot reply with "pong <id>".
  2. reminder   "remind me in 1 minute to canary <id>"         -> waits up to 150 s for the delivered "⏰" text.
     Skipped with --no-reminder, and skipped after a failed roundtrip (the bot is not answering at all).
It prints ONE JSON line on stdout: {"ok", "id", "checks": [{"name", "ok", "seconds", "detail"}], "error",
"cleaned", "seconds"}. Exit 0 when every check passed, 1 otherwise. Everything else goes to stderr.
After a clean pass it deletes the canary's own messages on both sides (revoke=True removes them from the owner's
and the bot's side of the private chat) so the owner's chat stays tidy. After a failure it leaves them for
diagnosis (--keep leaves them after a pass too). It never prints the session string, the api hash or the phone
number. The logic below imports no telethon, so the main test suite can import it with a fake client."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tclib  # noqa: E402

MARK = "[waggle-test] "
PONG_WAIT_S = 90.0
REMINDER_WAIT_S = 150.0
POLL_S = 2.0
DETAIL_CHARS = 160


def pong_question(cid):
    return f"canary {cid}: reply with exactly: pong {cid}"


def reminder_question(cid):
    return f"remind me in 1 minute to canary {cid}"


def is_pong(text, cid):
    return f"pong {cid}".lower() in " ".join((text or "").split()).lower()


def is_reminder(text, cid):
    """The delivered reminder reads "⏰ canary <id>" (optionally behind the bee). An acknowledgement that merely
    mentions the id does not count."""
    t = (text or "").strip()
    return "⏰" in t[:8] and cid.lower() in t.lower()


def _body(m):
    return (getattr(m, "raw_text", "") or "").strip()


def _result(name, ok, seconds, detail=""):
    return {"name": name, "ok": bool(ok), "seconds": round(float(seconds), 1), "detail": detail}


async def wait_for(client, bot, sent, match, wait_s, owned, *, poll_s=POLL_S, clock=time.monotonic, sleep=None):
    """Poll the chat until a bot message after `sent` satisfies match(text). Returns (matched message or None,
    seconds waited, the bot texts seen). Every bot message seen is recorded in `owned` (id -> {"text", "reply_to"})
    so cleanup can pick out the ones that belong to this canary."""
    sleep = sleep or asyncio.sleep
    t0 = clock()
    seen = {}
    while True:
        for m in reversed(await client.get_messages(bot, limit=30, min_id=sent.id)):
            if getattr(m, "sender_id", None) != bot.id:
                continue
            body = _body(m)
            seen[m.id] = body
            owned[m.id] = {"text": body, "reply_to": getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)}
            if body and match(body):
                return m, clock() - t0, seen
        if clock() - t0 >= wait_s:
            return None, clock() - t0, seen
        await sleep(poll_s)


def _others(seen, cid):
    """A short note on what the bot said instead (for the failure detail)."""
    texts = [t for t in seen.values() if t]
    return (" Instead it said: " + " | ".join(tclib.short_reply(t, 80) for t in texts[-2:])) if texts else ""


async def check_roundtrip(client, bot, cid, wait_s=PONG_WAIT_S, ours=None, owned=None, **kw):
    sent = await client.send_message(bot, MARK + pong_question(cid))
    (ours if ours is not None else []).append(sent.id)
    owned = owned if owned is not None else {}
    m, took, seen = await wait_for(client, bot, sent, lambda t: is_pong(t, cid), wait_s, owned, **kw)
    if m is not None:
        return _result("roundtrip", True, took, f"answered in {took:.1f} s")
    return _result("roundtrip", False, took,
                   f"Waggle didn't answer a test message within {wait_s:.0f} s." + _others(seen, cid))


async def check_reminder(client, bot, cid, wait_s=REMINDER_WAIT_S, ours=None, owned=None, **kw):
    sent = await client.send_message(bot, MARK + reminder_question(cid))
    (ours if ours is not None else []).append(sent.id)
    owned = owned if owned is not None else {}
    m, took, seen = await wait_for(client, bot, sent, lambda t: is_reminder(t, cid), wait_s, owned, **kw)
    if m is not None:
        return _result("reminder", True, took, f"delivered after {took:.1f} s")
    return _result("reminder", False, took,
                   f"A reminder asked for in 1 minute didn't arrive within {wait_s:.0f} s." + _others(seen, cid))


async def cleanup(client, bot, cid, ours, owned):
    """Delete our messages and the bot's messages that answer them or carry the canary id (other bot messages that
    happened to arrive meanwhile stay). Returns how many were removed, or None when it could not (a failure here
    never fails the check)."""
    mine = {i for i, o in owned.items() if o["reply_to"] in ours or cid.lower() in (o["text"] or "").lower()}
    ids = sorted(set(ours) | mine)
    if not ids:
        return 0
    try:
        await client.delete_messages(bot, ids, revoke=True)
        return len(ids)
    except Exception as e:  # noqa: BLE001
        print(f"cleanup failed: {type(e).__name__}", file=sys.stderr)
        return None


async def run(client, bot_name, *, reminder=True, cid=None, pong_wait=PONG_WAIT_S, reminder_wait=REMINDER_WAIT_S,
              keep=False, clock=time.monotonic, sleep=None, poll_s=POLL_S):
    """The whole check on an already built client. Returns the result dict (never raises)."""
    cid = cid or tclib.new_token()
    t0 = clock()
    out = {"ok": False, "id": cid, "checks": [], "error": "", "cleaned": None, "seconds": 0.0}
    ours, owned = [], {}
    kw = {"clock": clock, "sleep": sleep, "poll_s": poll_s}
    try:
        await client.connect()
        try:
            if not await client.is_user_authorized():
                out["error"] = "not_logged_in"
                out["checks"].append(_result("login", False, clock() - t0,
                                             "The test client is not logged in (run tools\\testclient\\login.py)."))
                return out
            bot = await client.get_entity(bot_name)
            first = await check_roundtrip(client, bot, cid, pong_wait, ours, owned, **kw)
            out["checks"].append(first)
            if first["ok"] and reminder:
                out["checks"].append(await check_reminder(client, bot, cid, reminder_wait, ours, owned, **kw))
            out["ok"] = all(c["ok"] for c in out["checks"])
            if out["ok"] and not keep:
                out["cleaned"] = await cleanup(client, bot, cid, ours, owned)
        finally:
            await client.disconnect()
    except Exception as e:  # noqa: BLE001  (a dead network, a locked session file, a flood wait)
        out["ok"] = False
        out["error"] = type(e).__name__
        out["checks"].append(_result("telegram", False, clock() - t0,
                                     f"The test client could not talk to Telegram: {type(e).__name__}: "
                                     f"{tclib.short_reply(str(e), DETAIL_CHARS)}"))
    out["seconds"] = round(clock() - t0, 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="")
    ap.add_argument("--no-reminder", action="store_true")
    ap.add_argument("--keep", action="store_true", help="leave the canary's messages in the chat after a pass")
    ap.add_argument("--pong-wait", type=float, default=PONG_WAIT_S)
    ap.add_argument("--reminder-wait", type=float, default=REMINDER_WAIT_S)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    d = tclib.state_dir()
    api = tclib.load_json(tclib.api_path(d))
    saved = tclib.load_json(tclib.target_path(d)) or {}
    bot = tclib.clean_username(a.bot) or tclib.clean_username(saved.get("bot_username"))
    if not api or not bot:
        res = {"ok": False, "id": "", "checks": [_result("login", False, 0, "The test client is not set up (no "
                                                         "api.json or bot username): run tools\\testclient\\login.py.")],
               "error": "not_set_up", "cleaned": None, "seconds": 0.0}
        print(json.dumps(res), flush=True)
        return 1
    try:
        from telethon import TelegramClient
    except ImportError:
        res = {"ok": False, "id": "", "checks": [_result("login", False, 0, "telethon is missing from the test "
                                                         "venv (run tools\\testclient\\setup.ps1).")],
               "error": "no_telethon", "cleaned": None, "seconds": 0.0}
        print(json.dumps(res), flush=True)
        return 1
    with tclib.SessionLock(d) as lock:
        if not lock.acquire():
            res = {"ok": False, "id": "", "checks": [_result("telegram", False, float(tclib.SESSION_WAIT_S),
                                                             "The test client was busy: another test-client run held "
                                                             f"the login for {tclib.SESSION_WAIT_S} s.")],
                   "error": "busy", "cleaned": None, "seconds": float(tclib.SESSION_WAIT_S)}
            print(json.dumps(res), flush=True)
            return 1
        client = TelegramClient(tclib.session_base(d), int(api["api_id"]), api["api_hash"])
        res = asyncio.run(run(client, bot, reminder=not a.no_reminder, pong_wait=a.pong_wait,
                              reminder_wait=a.reminder_wait, keep=a.keep))
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
