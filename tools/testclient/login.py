"""One-time Telegram login for the test client. Run with the test venv's python:
    %USERPROFILE%\\.waggle-testclient\\venv\\Scripts\\python.exe tools\\testclient\\login.py
Asks for api_id, api_hash, phone, the login code and the 2FA password (if any). Nothing secret is printed."""
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tclib  # noqa: E402


def main():
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    d = tclib.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tclib.restrict(d)
    api = tclib.load_json(tclib.api_path(d)) or {}
    try:
        api_id = int(api.get("api_id") or input("api_id (from my.telegram.org): ").strip())
    except ValueError:
        print("api_id must be a number.")
        return 1
    api_hash = api.get("api_hash") or getpass.getpass("api_hash (hidden): ").strip()
    tclib.save_json(tclib.api_path(d), {"api_id": api_id, "api_hash": api_hash})
    client = TelegramClient(tclib.session_base(d), api_id, api_hash)
    return asyncio.run(tclib.run_login(client, input, getpass.getpass, print, SessionPasswordNeededError, d))


if __name__ == "__main__":
    sys.exit(main())
