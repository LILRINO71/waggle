r"""Send one test message to Waggle as the owner and print every reply. The message goes to the owner's normal chat
with the bot, prefixed "[waggle-test]", so Waggle answers it but records it as origin "test" (nothing learns from
it). Run with the test venv's python:
    %USERPROFILE%\.waggle-testclient\venv\Scripts\python.exe tools\testclient\ask.py "what time is it?"
Options: --bot @username (saved after the first use), --wait 150 (max seconds), --settle 12 (seconds of quiet after
the last reply before stopping), --file photo.png (send a photo or document; the text becomes its marked
caption), --voice file.ogg (send a voice note instead of text; the mark goes in its caption
is not possible, so voice tests are NOT test-marked: use them sparingly). Exit 0 when at least one reply came."""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tclib  # noqa: E402

MARK = "[waggle-test] "


async def run(client, bot_name, text, wait_s, settle_s, voice=None, file=None, raw=False):
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("Not logged in: run login.py first.")
            return 1
        bot = await client.get_entity(bot_name)
        if voice:
            sent = await client.send_file(bot, voice, voice_note=True)
        elif file:  # a photo or document; the test mark goes in the caption
            sent = await client.send_file(bot, file, caption=MARK + text if text else MARK.strip())
        else:
            sent = await client.send_message(bot, text if raw else MARK + text)
        t0 = time.monotonic()
        last, seen = None, {}
        while time.monotonic() - t0 < wait_s:
            await asyncio.sleep(1.5)
            for m in reversed(await client.get_messages(bot, limit=30, min_id=sent.id)):
                if m.sender_id != bot.id:
                    continue
                body = (m.raw_text or "").strip() or ("<voice note>" if m.voice else "<media>")
                stamp = m.edit_date or m.date
                if seen.get(m.id) != (body, stamp):
                    first = m.id not in seen
                    seen[m.id] = (body, stamp)
                    last = time.monotonic()
                    print(f"[{last - t0:6.1f}s] {'new ' if first else 'edit'} #{m.id}: {body[:1500]}", flush=True)
            if last and time.monotonic() - last > settle_s:
                break
        if not seen:
            print(f"no reply within {wait_s}s")
            return 1
        return 0
    finally:
        await client.disconnect()


def main():
    from telethon import TelegramClient
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--bot", default="")
    ap.add_argument("--wait", type=float, default=150)
    ap.add_argument("--settle", type=float, default=12)
    ap.add_argument("--voice", default="")
    ap.add_argument("--file", default="", help="send this photo/document with the text as its caption")
    ap.add_argument("--raw", action="store_true", help="send the text without the test mark (slash commands only)")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    d = tclib.state_dir()
    api = tclib.load_json(tclib.api_path(d))
    saved = tclib.load_json(tclib.target_path(d)) or {}
    bot = tclib.clean_username(a.bot) or saved.get("bot_username") or ""
    if not api or not bot:
        print("Run login.py first, and pass --bot @username once.")
        return 1
    if a.bot:
        tclib.save_json(tclib.target_path(d), {**saved, "group_id": int(saved.get("group_id") or 0),
                                               "bot_username": bot})
    if not a.text and not a.voice and not a.file:
        print("Nothing to send.")
        return 1
    client = TelegramClient(tclib.session_base(d), int(api["api_id"]), api["api_hash"])
    if a.raw and not a.text.startswith("/"):
        print("--raw is only for slash commands: anything else must carry the test mark.")
        return 1
    return asyncio.run(run(client, bot, a.text, a.wait, a.settle, a.voice or None, a.file or None, a.raw))


if __name__ == "__main__":
    sys.exit(main())
