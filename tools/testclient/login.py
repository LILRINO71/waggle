"""One-time Telegram login for the test client. Run with the test venv's python:
    %USERPROFILE%\\.waggle-testclient\\venv\\Scripts\\python.exe tools\\testclient\\login.py
Default: log in by scanning a QR code with the Telegram app (no login code needed). `--code` uses the phone number
and a login code instead. Asks for api_id, api_hash and the 2FA password (if any). Nothing secret is printed."""
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
    show = None if "--code" in sys.argv[1:] else show_qr
    lock = tclib.SessionLock(d)
    if not lock.acquire(on_wait=lambda: print("waiting: another test-client run is using the login", flush=True)):
        print(f"another test-client run held the login for {tclib.SESSION_WAIT_S}s; try again later")
        return 1
    try:
        client = TelegramClient(tclib.session_base(d), api_id, api_hash)
        return asyncio.run(tclib.run_login(client, input, getpass.getpass, print, SessionPasswordNeededError, d,
                                           show_qr=show))
    finally:
        lock.release()
        (tclib.state_dir() / "login-qr.svg").unlink(missing_ok=True)  # the picture holds a short-lived login token


def show_qr(url):
    """Draw the login QR in the terminal and also open it as a picture (easier to scan)."""
    import os
    import qrcode
    import qrcode.image.svg
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    path = tclib.state_dir() / "login-qr.svg"
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathFillImage).save(str(path))
    try:
        os.startfile(str(path))
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
