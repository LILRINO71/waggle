"""Send a ping to the Waggle Lab group and wait up to 90 s for the bot's reply. Exit 0 = reply, 1 = none.
Run with the test venv's python after login.py:
    %USERPROFILE%\\.waggle-testclient\\venv\\Scripts\\python.exe tools\\testclient\\ping.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tclib  # noqa: E402


def main():
    from telethon import TelegramClient
    d = tclib.state_dir()
    api, target = tclib.load_json(tclib.api_path(d)), tclib.load_target(d)
    if not api or not target:
        print("Missing api.json or target.json: run login.py first.")
        return 1
    client = TelegramClient(tclib.session_base(d), int(api["api_id"]), api["api_hash"])
    return asyncio.run(tclib.run_ping(client, target, print))


if __name__ == "__main__":
    sys.exit(main())
