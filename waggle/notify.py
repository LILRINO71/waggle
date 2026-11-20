"""Everything Waggle sends goes through here: one decision, one outbox row, one evidence record per channel.

deliver(kind, ...)   the single way out. kind = text | voice | file | push | call. It decides (developer/test
                     activity is suppressed, non-urgent messages wait out quiet hours, rows expire after
                     outbox_max_age_h, one idempotency key per decision or reminder) and writes exactly one row to the
                     core outbox (waggle.core.outbox in runtime.db). It never sends: the core's sender loop sends due
                     rows and retries failures, so nothing in this module calls a transport.
push_sender(row)     the ntfy transport, registered with the core sender for kind "push" (tick.register_core);
                     call.call_sender is the one for kind "call". Telegram text/voice/file senders live elsewhere.
Evidence             thin wrappers over the capability ledger (waggle.core.ledger): a channel is ready only after a
                     recorded end-to-end success within its time limit with no failure after it. Weaker facts
                     (ntfy accepted a post, a Twilio account looks set up) are recorded under separate names and
                     never make a channel ready.

send(), to_phone(), to_ntfy(), voice() and unseen() keep working for the other modules.
"""
import datetime as dt
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import events
from .config import Settings, data_path

KINDS = ("text", "voice", "file", "push", "call")
CHANNEL = {"text": "telegram_text", "voice": "telegram_voice", "file": "telegram_file", "push": "ntfy", "call": "call"}
CHANNELS = ("telegram_text", "telegram_voice", "ntfy", "call")
# Delivery the provider confirmed for the user's own chat/phone counts as end-to-end for these channels. An ntfy
# post only proves the server took it (the phone may not be subscribed), so ntfy needs the user's confirmation.
NEEDS_CONFIRMATION = {"ntfy"}
# Unprompted outreach: these origins count against the daily cap; replies, reminders the user asked for, reports
# of work the user started and the scheduled notes do not.
INITIATIVE_ORIGINS = ("initiative", "followup", "scout")


# ---- quiet hours -----------------------------------------------------------------------------------

def in_quiet_hours(settings=None, now=None):
    s = settings or Settings.load()
    start, end = s.quiet_hours
    h = (now if isinstance(now, dt.datetime) else dt.datetime.fromtimestamp(now) if now else dt.datetime.now()).hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def quiet_ends(settings=None, now=None):
    """Timestamp when the current quiet period ends (now if it isn't quiet)."""
    s = settings or Settings.load()
    now = now or time.time()
    if not in_quiet_hours(s, now):
        return now
    t = dt.datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
    for _ in range(25):
        t += dt.timedelta(hours=1)
        if not in_quiet_hours(s, t):
            return t.timestamp()
    return now


def is_dev_source(source, settings=None):
    """Developer one-shots and tests (Hermes session source oneshot/cli/tool/...) never reach the phone."""
    if not source:
        return False
    s = settings or Settings.load()
    return str(source).lower() in {x.lower() for x in s.dev_sources}


# ---- raw senders (kept public: other modules and tests use them) ------------------------------------

def _toast(text):
    if os.name != "nt":
        return
    safe = text.replace("'", "''")[:240]
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
          "|Out-Null;$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
          f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('Waggle: {safe}'))|Out-Null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Waggle')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def hermes_exe():
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    exe = home / "bin" / ("hermes.exe" if os.name == "nt" else "hermes")
    return exe if exe.exists() else None


def to_phone(text, settings=None):
    """The raw `hermes send` call (works without the gateway running). True on success. Not used by deliver():
    pushsetup keeps it for its one-off test message. MEDIA:<path> lines attach files."""
    s = settings or Settings.load()
    exe = hermes_exe()
    if not exe:
        return False
    env = dict(os.environ, HERMES_HOME=str(exe.parent.parent))
    try:
        r = subprocess.run([str(exe), "send", "-q", "-t", s.phone_target, text[:3500]], capture_output=True,
                           text=True, timeout=60, env=env, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ntfy_topic(settings=None):
    """The private ntfy channel (random, created once). Anyone who knows it can read alerts, so it is long."""
    s = settings or Settings.load()
    if not s.ntfy_topic:
        import secrets
        s.ntfy_topic = "waggle-" + secrets.token_hex(12)
        s.save()
    return s.ntfy_topic


def to_ntfy(text, title="Waggle", priority=5, tags=("bee",), settings=None, post=None):
    """Push to the ntfy app (iPhone/Android). priority 5 = urgent: on iPhone a Time-Sensitive alert that breaks
    through Focus; 3 = normal. Free, no account. True on success."""
    import urllib.request
    s = settings or Settings.load()
    body = {"topic": ntfy_topic(s), "title": title[:120], "message": text[:3500], "priority": int(priority),
            "tags": list(tags)}
    if post:
        return post(s.ntfy_server, body)
    try:
        req = urllib.request.Request(s.ntfy_server, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def ntfy_publish(text, *, title="Waggle", priority=3, tags=("bee",), settings=None, post=None):
    """One ntfy post; returns ntfy's message id. Raises SendError: permanent for a refused request (4xx other than
    429), with retry_after for 429, retryable for anything else. post(url, body) -> response dict, for tests."""
    import urllib.error
    import urllib.request
    s = settings or Settings.load()
    body = {"topic": ntfy_topic(s), "title": (title or "Waggle")[:120], "message": (text or "")[:3500],
            "priority": int(priority), "tags": list(tags)}
    try:
        if post:
            res = post(s.ntfy_server, body)
        else:
            req = urllib.request.Request(s.ntfy_server, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SendError("ntfy rate limit (HTTP 429)", retry_after=float(e.headers.get("Retry-After") or 60)) from e
        raise SendError(f"ntfy refused the post (HTTP {e.code})", permanent=400 <= e.code < 500) from e
    except SendError:
        raise
    except Exception as e:  # noqa: BLE001  (network down, timeout, bad JSON)
        raise SendError(f"ntfy post failed: {type(e).__name__}: {e}"[:200]) from e
    mid = (res or {}).get("id") if isinstance(res, dict) else None
    if not mid:
        raise SendError("ntfy answered without a message id")
    return str(mid)


def push_sender(row, *, settings=None, post=None):
    """The core sender's transport for kind "push": returns ntfy's message id or raises SendError. A post ntfy
    accepted is recorded as the weaker fact "ntfy.accepted" (the phone may not be subscribed); only the user's
    confirmation makes the "ntfy" channel ready."""
    meta = row.get("meta") or {}
    urgent = bool(meta.get("urgent"))
    ref = f"outbox:{row.get('id')}"
    try:
        mid = ntfy_publish(row.get("body") or "", title=meta.get("title") or "Waggle", priority=5 if urgent else 3,
                           tags=("rotating_light",) if urgent else ("bee",), settings=settings, post=post)
    except SendError as e:
        record_evidence("ntfy", False, f"push failed: {e}", ref=ref)
        raise
    record_evidence("ntfy", True, "the ntfy server accepted the post", ref=f"{ref} ntfy:{mid}")
    return mid


# ---- evidence (waggle.core.ledger) ---------------------------------------------------------------------

# How long one end-to-end success keeps a channel ready (the ledger's ttl).
CHANNEL_TTL_S = {"telegram_text": 2 * 86400, "telegram_voice": 7 * 86400, "telegram_file": 7 * 86400,
                 "ntfy": 30 * 86400, "call": 30 * 86400}
NAMES = {"telegram_text": "Telegram text", "telegram_voice": "Telegram voice note", "ntfy": "Urgent push (ntfy)",
         "call": "Phone call"}


def ledger_name(channel, ok=True, level="delivered"):
    """The ledger entry a record goes to. A delivery-level success on a channel that needs the user's confirmation
    is only the weaker fact "<channel>.accepted"; it never makes the channel itself ready."""
    if ok and level != "confirmed" and channel in NEEDS_CONFIRMATION:
        return f"{channel}.accepted"
    return channel


def record_evidence(channel, ok, how, ref="", level="delivered", now=None, ttl_s=None):
    """Record one check of a channel in the capability ledger. level: delivered (the provider confirmed delivery
    to the user's own chat/phone, with its message id) or confirmed (the user answered or said they got it).
    Returns the ledger name it was recorded under."""
    from waggle.core import ledger
    name = ledger_name(channel, ok, level)
    how = " ".join(str(how or "").split())[:200] or ("ok" if ok else "failed")
    ledger.record(name, bool(ok), method=how, evidence_ref=str(ref or "")[:200], detail=how, now=now,
                  ttl_s=ttl_s if ttl_s is not None else CHANNEL_TTL_S.get(channel))
    return name


def confirm_channel(channel, how="the user said they received it", now=None):
    return record_evidence(channel, True, how, level="confirmed", now=now)


def _when(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _ledger_row(name, now=None):
    from waggle.core import ledger
    return next((r for r in ledger.status(now) if r["name"] == name), None)


def readiness(channel, now=None):
    """{"channel", "state": verified | failing | not verified, "ready", "line"}, from the ledger only: verified
    means a recorded end-to-end success within its time limit and no failure after it."""
    row = _ledger_row(channel, now)
    ok_at = row and row["verified_at"]
    bad_at = row and row["last_failure_at"]
    if row and row["ready"]:
        state, line = "verified", f"verified {_when(ok_at)}: {row['method']}"
    elif bad_at and (not ok_at or bad_at >= ok_at):
        state, line = "failing", f"failing since {_when(bad_at)}: {row['last_failure']}"
    elif ok_at:
        state, line = "not verified", f"last verified {_when(ok_at)}, longer ago than its time limit"
    else:
        state, line = "not verified", "never delivered end to end"
        weak = _ledger_row(ledger_name(channel, True, "delivered"), now) if channel in NEEDS_CONFIRMATION else None
        if weak and weak["verified_at"]:
            line = f"sent {_when(weak['verified_at'])} but nobody has confirmed it arrived on the phone"
    return {"channel": channel, "state": state, "ready": state == "verified", "line": line}


def readiness_lines():
    """One line per channel for `waggle doctor` and the HQ note. For calls, the provider check is a separate,
    weaker fact: it can say a call is impossible, never that calls work."""
    out = []
    for ch in CHANNELS:
        r = readiness(ch)
        line = f"{NAMES[ch]}: {r['state']} ({r['line']})"
        if ch == "call":
            try:
                from . import call
                p = call.provisioned()
                if not p["ready"]:
                    line += f". Not possible yet: {p['missing']}"
            except Exception:  # noqa: BLE001
                pass
        out.append(line)
    return out


# ---- the outbox (waggle.core.outbox) ------------------------------------------------------------------

class SendError(Exception):
    """Raised by a registered sender to fail one attempt. permanent=True stops retries; retry_after (seconds)
    delays the next attempt."""

    def __init__(self, message, *, permanent=False, retry_after=None):
        super().__init__(message)
        self.permanent = bool(permanent)
        if retry_after is not None:
            self.retry_after = float(retry_after)


def _priority(urgent, asked):
    return 10 if urgent else 5 if asked else 0


def _view(row):
    """An outbox row as callers here use it: the core row plus `status` (its state), `text`, `held` and the
    delivery details from meta."""
    if row is None:
        return None
    meta = row.get("meta") or {}
    return {**row, "status": row["state"], "text": row.get("body", ""), "held": bool(meta.get("held")),
            "topic": meta.get("topic", ""), "title": meta.get("title", ""), "urgent": bool(meta.get("urgent")),
            "asked": bool(meta.get("asked")), "decision_id": meta.get("decision_id")}


def deliver(kind, text="", *, path="", title="", urgent=False, asked=False, origin="manual", source=None, topic="",
            decision_id=None, expires=None, settings=None, now=None, chat="", ref=None):
    """Decide whether and when a message may go out, then write exactly one outbox row. Returns that row (see
    _view), or a row with status 'suppressed' (and id None) when developer/test activity caused it.

    Nothing is sent here: the core's sender loop sends due rows and retries failures. Non-urgent messages wait
    out quiet hours (not_before); urgent ones, things the user asked for and pushes do not. A row expires after
    outbox_max_age_h unless `expires` says otherwise. The idempotency key is "<origin>:<ref>" (ref defaults to the
    decision id plus the kind), so the same decision or reminder never queues two messages.
    asked: the user asked for this (a reminder, "call me at 7"): it bypasses the daily cap like urgent items do.
    """
    from waggle.core import outbox, store
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    s = settings or Settings.load()
    now = store.now_or(now)
    if is_dev_source(source, s):
        with store.transaction() as con:
            store.insert_event(con, "outreach.suppressed", source="notify", now=now,
                               data={"kind": kind, "origin": origin, "cause": source, "text": (text or "")[:120]})
        events.log("outbox", msg_kind=kind, origin=origin, status="suppressed", cause=source, text=(text or "")[:120])
        return {"id": None, "state": "suppressed", "status": "suppressed", "kind": kind, "body": text or "",
                "text": text or "", "origin": origin, "held": False, "meta": {}}
    held = not urgent and not asked and kind != "push" and in_quiet_hours(s, now)
    not_before = quiet_ends(s, now) if held else None
    exp = expires if expires is not None else (not_before or now) + s.outbox_max_age_h * 3600
    if ref is None:
        ref = f"{decision_id}:{kind}" if decision_id else uuid.uuid4().hex[:12]
    meta = {"title": title or "", "topic": topic or "", "decision_id": decision_id, "urgent": bool(urgent),
            "asked": bool(asked), "held": held}
    if source:
        meta["cause"] = str(source)
    oid = outbox.enqueue(kind, text or "", chat=chat or "", file=str(path) if path else None,
                         idem_key=f"{origin}:{ref}", priority=_priority(urgent, asked), origin=origin,
                         not_before=not_before, expires=exp, meta=meta, now=now)
    events.log("outbox", id=oid, msg_kind=kind, origin=origin, status="queued", urgent=bool(urgent), held=held,
               text=(text or "")[:120])
    return get(oid)


def get(oid):
    from waggle.core import outbox
    return _view(outbox.get(oid)) if oid is not None else None


def rows(since=0.0, status=None, origin=None, limit=1000):
    """Outbox rows created at or after `since`, oldest first. status: a state or a tuple of states;
    origin: a tuple of origins."""
    from waggle.core import outbox
    states = (status,) if isinstance(status, str) else status
    out = [_view(r) for r in outbox.list(limit=limit) if (r.get("created") or 0) >= since]
    out = [r for r in out if (not states or r["state"] in states) and (not origin or r["origin"] in origin)]
    return sorted(out, key=lambda r: r["id"])


# ---- voice notes ---------------------------------------------------------------------------------------

def _synth(text, out, settings=None):
    """Kokoro to an Ogg/Opus file: the warm server on 127.0.0.1:8899 first, else kokoro_say.py in its own venv.
    True when the file was written."""
    import urllib.request
    s = settings or Settings.load()
    body = json.dumps({"text": text, "out": str(out), "voice": s.voice_note_voice,
                       "speed": s.voice_note_speed}).encode()
    try:
        req = urllib.request.Request("http://127.0.0.1:8899/", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if json.loads(r.read()).get("ok") and Path(out).exists():
                return True
    except Exception:  # noqa: BLE001  (server not running: cold synthesis below)
        pass
    py = Path.home() / ".waggle" / "kokoro" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = Path(__file__).resolve().parent.parent / "voice" / "kokoro_say.py"
    if not py.exists() or not script.exists():
        return False
    try:
        subprocess.run([str(py), str(script), "--text", text, "--out", str(out), "--voice", s.voice_note_voice,
                        "--speed", str(s.voice_note_speed)], capture_output=True, timeout=180,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return Path(out).exists()


def voice(text, settings=None, urgent=False, asked=False, origin="manual", topic="", decision_id=None, caption=None,
          now=None, synth=None, source=None, chat="", ref=None):
    """Say `text` as a Telegram voice note (with the words as text underneath). Falls back to a plain text message
    when synthesis fails, so the content is never lost."""
    s = settings or Settings.load()
    spoken = " ".join((text or "").split())[:600]
    folder = data_path("voice_out")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{uuid.uuid4().hex[:10]}.ogg"
    ok = False
    if not is_dev_source(source, s):
        try:
            ok = (synth or _synth)(spoken, out, s)
        except Exception:  # noqa: BLE001
            ok = False
    kw = dict(urgent=urgent, asked=asked, origin=origin, topic=topic, decision_id=decision_id, settings=s, now=now,
              source=source, chat=chat, ref=ref)
    if ok:
        return deliver("voice", caption if caption is not None else text, path=str(out), **kw)
    return deliver("text", "🐝 " + (caption or text), **kw)


# ---- the classic notify call ---------------------------------------------------------------------------

def send(text, urgent=False, settings=None, phone=True, push=None, source=None, origin="notify"):
    """Queue for your next message + desktop toast + the phone (through the outbox; non-urgent messages wait out
    quiet hours). Urgent also goes out as an ntfy alarm unless push=False. source = the Hermes session source that
    caused this; developer/test sources never reach the phone."""
    s = settings or Settings.load()
    quiet = in_quiet_hours(s)
    dev = is_dev_source(source, s)
    row = {"time": time.time(), "text": text, "urgent": urgent, "seen": False}
    if urgent or not quiet:
        _toast(text)
    if phone:
        r = deliver("text", f"🐝 {text}", urgent=urgent, origin=origin, source=source, settings=s)
        row["queued"] = r["status"] == "queued"
        row["outbox"] = r["id"]
        row["held"] = bool(r["held"])
    if (urgent if push is None else push) and not dev:
        r = deliver("push", text, title="🚨 Waggle needs you", urgent=True, origin=origin, settings=s)
        row["push_outbox"] = r["id"]
    with open(data_path("notifications.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    events.log("notify", text=text[:200], urgent=urgent, quiet=quiet, queued=row.get("queued", False),
               pushed="push_outbox" in row, dev=dev, origin=origin)
    return row


def unseen(mark=True):
    p = data_path("notifications.jsonl")
    if not p.exists():
        return []
    rows_ = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    new = [r for r in rows_ if not r.get("seen")]
    if mark and new:
        for r in rows_:
            r["seen"] = True
        p.write_text("".join(json.dumps(r) + "\n" for r in rows_), encoding="utf-8")
    return new


def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args[:1] == ["confirm"] and len(args) > 1:
        print(json.dumps(confirm_channel(args[1], " ".join(args[2:]) or "the user said they received it")))
    else:
        print("\n".join(readiness_lines()))
    return 0


if __name__ == "__main__":
    main()
