"""Dead-man switch client: tells a free Cloudflare Worker (cloud/deadman/) that Waggle is alive, and lets the Worker
deliver reminders while the PC is off.

    python -m waggle.deadman status         # is it set up, when the last beat went out, what the Worker knows
    python -m waggle.deadman setup          # make the shared token and print the owner's steps (docs/DEADMAN.md)
    python -m waggle.deadman beat           # send a beat now (the core does this every 5 minutes)
    python -m waggle.deadman seturl <url>   # write deadman_url to config (tools/deadman-setup.ps1 does this)

Inert until configured: with `deadman_url` empty (or no token in Credential Manager) nothing here touches the network.

What goes to the Worker: a beat with the open reminders due in the next 48 h (id, due time, the reminder text, cut
to 200 characters; deadman_hide_text sends "Reminder due" instead) and the id of each reminder Waggle delivered.
Nothing else. The shared token lives in Windows Credential Manager (target waggle-deadman-token); the Telegram token,
chat id and ntfy topic are copied to the Worker as Worker secrets by the setup script and are never in the repo.

No doubles: the Worker sends a reminder only 3 minutes after it was due, and only if the PC has not acked it. The core
acks right after it delivers (reminder_due), and also asks the Worker first whether it already sent the reminder
(worker_sent), because a reminder that fell due while the PC was off still fires late at the next start.
A beat's reply lists reminders the Worker already sent; the PC marks them done and drops their timers.
"""
import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import wincred
from .config import Settings

CRED = "waggle-deadman-token"
JOB_KIND = "deadman.beat"
TIMER = "deadman.beat"
INTERVAL = "5m"
HORIZON_S = 48 * 3600  # reminders due within this long go to the Worker
OVERDUE_S = 24 * 3600  # an open reminder this late is still listed (the Worker drops anything older)
TEXT_MAX = 200
UA = "waggle-deadman/1"
META_OK, META_ERR, META_SENT = "deadman.last_ok", "deadman.last_error", "deadman.worker_sent_total"


class DeadmanError(Exception):
    pass


# ---- configuration -------------------------------------------------------------------------------------------------

def base_url(settings=None):
    """The Worker's address without a trailing slash, or "" when not set (or not https, except localhost)."""
    s = settings or Settings.load()
    url = (getattr(s, "deadman_url", "") or "").strip().rstrip("/")
    if not url:
        return ""
    host = urllib.parse.urlparse(url).hostname or ""
    if url.startswith("https://") or (url.startswith("http://") and host in ("localhost", "127.0.0.1")):
        return url
    return ""


def token():
    found = wincred.read(CRED)
    return (found[1] or "").strip() if found else ""


def configured(settings=None):
    return bool(base_url(settings) and token())


# ---- talking to the Worker -----------------------------------------------------------------------------------------

def _call(method, path, body=None, *, settings=None, timeout=10, urlopen=None):
    """One authenticated request; returns the parsed JSON answer. Raises DeadmanError without the token in it."""
    url, tok = base_url(settings), token()
    if not url or not tok:
        raise DeadmanError("not configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with (urlopen or urllib.request.urlopen)(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise DeadmanError(f"the Worker answered HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001  (offline, DNS, timeout)
        raise DeadmanError(f"could not reach the Worker ({type(e).__name__})") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise DeadmanError("the Worker's answer was not JSON") from None


def upcoming(now=None, settings=None):
    """The open reminders the Worker should hold: [{"id", "at" (epoch s), "text"}], soonest first. Only the user's
    own reminders (the others never reach the phone)."""
    from . import followups, outredact
    s = settings or Settings.load()
    now = time.time() if now is None else float(now)
    hide = bool(getattr(s, "deadman_hide_text", False))
    out = []
    for it in followups.items("open", "reminder"):
        due = float(it.get("due") or 0)
        if (it.get("origin") or "user") != "user" or not (now - OVERDUE_S <= due <= now + HORIZON_S):
            continue
        text = "Reminder due" if hide else outredact.scrub(" ".join(str(it.get("what") or "").split()), "deadman")
        out.append({"id": it["id"], "at": due, "text": (text or "Reminder due")[:TEXT_MAX]})
    return sorted(out, key=lambda r: r["at"])


def _apply_sent(ids, now):
    """Reminders the Worker already sent: mark them done and drop their timers so they do not fire again."""
    from . import followups
    from .core import timers
    done = []
    for fid in ids:
        item = followups.get(str(fid))
        if not item or item["status"] != "open":
            continue
        if item.get("timer_id"):
            timers.remove(item["timer_id"])
        followups.mark(item["id"], "sent", now, None, "sent by the backup service while the PC was off")
        done.append(item["id"])
    return done


def beat(now=None, settings=None, urlopen=None):
    """Send a beat. Never raises. {"skipped": why} when not configured, {"error": why} when it failed, else
    {"ok": True, "reminders": n, "worker_sent": [ids marked done]}."""
    from .core import store
    now = time.time() if now is None else float(now)
    s = settings or Settings.load()
    if not configured(s):
        return {"skipped": "not configured"}
    try:
        reminders = upcoming(now, s)
        res = _call("POST", "/beat", {"reminders": reminders}, settings=s, urlopen=urlopen)
        done = _apply_sent(res.get("sent") or [], now)
        store.set_meta(META_OK, now)
        store.set_meta(META_ERR, "")
        if done:
            store.set_meta(META_SENT, int(float(store.get_meta(META_SENT, 0) or 0)) + len(done))
        return {"ok": True, "reminders": len(reminders), "worker_sent": done}
    except Exception as e:  # noqa: BLE001  (a beat must never break the core)
        why = str(e) if isinstance(e, DeadmanError) else f"{type(e).__name__}: {e}"
        try:
            store.set_meta(META_ERR, f"{why} at {int(now)}")
        except Exception:  # noqa: BLE001
            pass
        return {"error": why[:200]}


def ack(fid, settings=None, urlopen=None):
    """Tell the Worker a reminder was delivered (so it will not send it). True when it took it. Never raises."""
    s = settings or Settings.load()
    if not configured(s):
        return False
    try:
        _call("POST", "/ack", {"id": str(fid)}, settings=s, timeout=4, urlopen=urlopen)
        return True
    except Exception:  # noqa: BLE001  (the next beat covers it)
        return False


def worker_sent(fid, settings=None, urlopen=None):
    """True when the Worker already sent this reminder. False on any doubt (not configured, offline): then the
    PC delivers it itself. Never raises."""
    s = settings or Settings.load()
    if not configured(s):
        return False
    try:
        res = _call("GET", "/sent?id=" + urllib.parse.quote(str(fid), safe=""), settings=s, timeout=4, urlopen=urlopen)
        return bool(res.get("sent"))
    except Exception:  # noqa: BLE001
        return False


# ---- the core timer ------------------------------------------------------------------------------------------------

def beat_job(payload=None, *, settings=None, now=None):
    """Job deadman.beat. Cheap and quiet when not configured; a failed beat is recorded, never raised."""
    return beat(now, settings)


def register_core(now=None):
    from .core import jobs, timers
    jobs.register(JOB_KIND, beat_job, max_attempts=1, lease_s=60, timeout_s=60)
    timers.add(TIMER, "interval", INTERVAL, JOB_KIND, {}, catch_up="skip", origin="deadman", now=now)
    return {"jobs": [JOB_KIND], "timers": [TIMER]}


# ---- command line --------------------------------------------------------------------------------------------------

def _age(ts, now):
    try:
        secs = max(0, int(now - float(ts)))
    except (TypeError, ValueError):
        return "never"
    return f"{secs // 60} min ago" if secs >= 60 else f"{secs} s ago"


def status(settings=None, now=None, urlopen=None):
    """A dict about the set-up: what is configured, the last beat, and (when reachable) what the Worker holds."""
    from .core import store
    s = settings or Settings.load()
    now = time.time() if now is None else float(now)
    out = {"url": base_url(s), "token": bool(token()), "configured": configured(s),
           "last_ok": store.get_meta(META_OK), "last_error": store.get_meta(META_ERR) or "",
           "worker_sent_total": int(float(store.get_meta(META_SENT, 0) or 0))}
    out["last_ok_age"] = _age(out["last_ok"], now) if out["last_ok"] else "never"
    if out["configured"]:
        try:
            out["worker"] = _call("GET", "/status", settings=s, urlopen=urlopen)
        except DeadmanError as e:
            out["worker_error"] = str(e)
    return out


def setup(new_token=False):
    """Make the shared token (once; never printed) and return the owner's steps as text."""
    made = False
    if new_token or not token():
        wincred.write(CRED, "waggle", secrets.token_urlsafe(32))
        made = True
    return "\n".join([
        "The shared token is " + ("made and kept in Windows Credential Manager." if made else "already in Credential Manager."),
        "Nothing is sent anywhere until the Worker is deployed. Steps (full text: docs/DEADMAN.md):",
        "  1. Make a free Cloudflare account at https://dash.cloudflare.com/sign-up (no card).",
        "  2. In PowerShell:  cd <the Waggle folder>\\cloud\\deadman  then  npx wrangler login   (a browser opens, click Allow)",
        "  3. In PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\\deadman-setup.ps1",
        "  4. Check:  python -m waggle.deadman status",
    ])


def seturl(url):
    url = (url or "").strip().rstrip("/")
    probe = Settings(deadman_url=url)
    if not base_url(probe):
        raise DeadmanError("the address must start with https://")
    s = Settings.load()
    s.deadman_url = url
    s.save()
    return url


def secret_value(name):
    """A value for the setup script to pipe into `wrangler secret put`. Never for a terminal."""
    from . import notify
    from .core import telegram
    if name == "token":
        value = token()
    elif name == "telegram_token":
        value = telegram.load_token()
    elif name == "telegram_chat":
        value = telegram.home_chat() or ""
    elif name == "ntfy_topic":
        value = notify.ntfy_topic()
    else:
        raise DeadmanError(f"unknown secret {name!r}")
    if not value:
        raise DeadmanError(f"{name} is not set on this PC")
    return value


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m waggle.deadman", description="Dead-man switch client.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="set-up and last beat")
    p = sub.add_parser("setup", help="make the shared token and print the steps")
    p.add_argument("--new-token", action="store_true", help="replace the token (then redeploy the Worker)")
    sub.add_parser("beat", help="send a beat now")
    p = sub.add_parser("seturl", help="write deadman_url to config")
    p.add_argument("url")
    p = sub.add_parser("secret", help="print a secret for piping into wrangler (refuses a terminal)")
    p.add_argument("name", choices=["token", "telegram_token", "telegram_chat", "ntfy_topic"])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "setup":
            print(setup(a.new_token))
        elif a.cmd == "seturl":
            print("deadman_url set to", seturl(a.url))
        elif a.cmd == "secret":
            if sys.stdout.isatty():
                print("This prints a secret; pipe it (| npx wrangler secret put NAME) instead of showing it.",
                      file=sys.stderr)
                return 2
            sys.stdout.write(secret_value(a.name))
        elif a.cmd == "beat":
            res = beat()
            print(json.dumps(res))
            return 0 if res.get("ok") else 1
        else:
            st = status()
            print("Set up:      " + ("yes" if st["configured"] else "no (deadman_url empty or no token; run: python -m waggle.deadman setup)"))
            if st["url"]:
                print("Worker:      " + st["url"])
            print("Last beat:   " + st["last_ok_age"] + (f"   last error: {st['last_error']}" if st["last_error"] else ""))
            w = st.get("worker")
            if w:
                age = w.get("age_s")
                print(f"Worker has:  last beat {age} s ago, {w.get('queued')} reminder(s) queued, "
                      f"Telegram {'ok' if w.get('telegram') else 'MISSING'}, ntfy {'ok' if w.get('ntfy') else 'MISSING'}"
                      + (", DOWN alert sent" if w.get("down") else ""))
            elif st.get("worker_error"):
                print("Worker:      " + st["worker_error"])
            print(f"Worker has sent {st['worker_sent_total']} reminder(s) for the PC so far.")
        return 0
    except DeadmanError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
