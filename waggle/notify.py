"""Everything Waggle sends goes through here: one outbox, one delivery function, one evidence record per channel.

deliver(kind, ...)   the single way out. kind = text | voice | file | push | call. The message is written to the
                     outbox (~/.waggle/outbox.db) first, then sent through the transport; a failure is retried with
                     backoff (1, 2, 5, 10, 30, 60 min) by flush(), which the minute tick runs, until it is sent or
                     expires (outbox_max_age_h). Every attempt and its result is recorded, so nothing is lost
                     silently. Non-urgent messages wait out quiet hours and go out together in the morning.
Transport            the raw senders (send_text, send_voice, send_file, push, call). Today: `hermes send` for
                     Telegram, ntfy.sh for push, Twilio for calls. set_transport() re-hosts delivery without
                     touching the logic.
Evidence             channel_evidence.json: per channel the last end-to-end success (when, how verified) and the
                     last failure (when, why). readiness() derives "verified / failing / not verified" from it,
                     never from configuration being present.

send(), to_phone(), to_ntfy(), voice() and unseen() keep working for the other modules.
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from .config import Settings, data_path

KINDS = ("text", "voice", "file", "push", "call")
CHANNEL = {"text": "telegram_text", "voice": "telegram_voice", "file": "telegram_file", "push": "ntfy", "call": "call"}
CHANNELS = ("telegram_text", "telegram_voice", "ntfy", "call")
# Delivery the provider confirmed for the user's own chat/phone counts as end-to-end for these channels. An ntfy
# post only proves the server took it (the phone may not be subscribed), so ntfy needs the user's confirmation.
NEEDS_CONFIRMATION = {"ntfy"}
BACKOFF = (60, 120, 300, 600, 1800, 3600)
LEASE_S = 300
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

_last_error = {"text": ""}


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
    """Send to the phone through `hermes send` (works without the gateway running). True on success.
    MEDIA:<path> lines attach files; with [[audio_as_voice]] an .ogg goes out as a voice bubble."""
    s = settings or Settings.load()
    exe = hermes_exe()
    if not exe:
        _last_error["text"] = "hermes not found"
        return False
    env = dict(os.environ, HERMES_HOME=str(exe.parent.parent))
    try:
        r = subprocess.run([str(exe), "send", "-q", "-t", s.phone_target, text[:3500]], capture_output=True,
                           text=True, timeout=90, env=env, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        _last_error["text"] = "" if r.returncode == 0 else (
            f"hermes send exit {r.returncode}: " + " ".join(((r.stderr or "") + (r.stdout or "")).split())[-200:])
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        _last_error["text"] = "hermes send timed out"
        return False
    except OSError as e:
        _last_error["text"] = f"hermes send failed: {e}"[:200]
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


# ---- transport ---------------------------------------------------------------------------------------

class Transport:
    """The raw senders behind deliver(). Each returns (ok, detail). Swap it with set_transport() to re-host
    delivery (an always-on process, another chat app) without touching the outbox or the decision logic."""

    def send_text(self, text, settings):
        ok = to_phone(text, settings)
        return ok, "Telegram accepted it (hermes send)" if ok else (_last_error["text"] or "hermes send failed")

    def send_voice(self, path, caption, settings):
        if not path or not Path(path).exists():
            return False, "voice file missing"
        msg = f"[[audio_as_voice]]\nMEDIA:{path}" + (f"\n{caption}" if caption else "")
        ok = to_phone(msg, settings)
        return ok, "Telegram accepted the voice note (hermes send)" if ok else (_last_error["text"] or "send failed")

    def send_file(self, path, caption, settings):
        if not path or not Path(path).exists():
            return False, "file missing"
        ok = to_phone((caption + "\n" if caption else "") + f"MEDIA:{path}", settings)
        return ok, "Telegram accepted the file (hermes send)" if ok else (_last_error["text"] or "send failed")

    def push(self, text, title, priority, settings):
        ok = to_ntfy(text, title=title, priority=priority, tags=("rotating_light",) if priority >= 5 else ("bee",),
                     settings=settings)
        return ok, "ntfy server accepted it" if ok else "ntfy post failed"

    def call(self, text, settings):
        from . import call
        res = call.place(text, settings=settings)
        return res["ok"], res.get("why", "") + (f" sid={res['sid']}" if res.get("sid") else "")


_transport = None


def get_transport():
    global _transport
    if _transport is None:
        _transport = Transport()
    return _transport


def set_transport(t):
    """Install another transport (None = the default). Returns the previous one."""
    global _transport
    prev, _transport = _transport, t
    return prev


# ---- evidence ----------------------------------------------------------------------------------------

def _evidence_path():
    return data_path("channel_evidence.json")


def _load_evidence():
    try:
        return json.loads(_evidence_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_evidence(channel, ok, how, ref="", level="delivered", now=None):
    """level: delivered (the provider confirmed delivery to the user's own chat/phone) or confirmed (the user
    answered or said they got it)."""
    ev = _load_evidence()
    row = ev.setdefault(channel, {})
    stamp = {"time": now or time.time(), "how": str(how)[:200], "ref": str(ref)[:80]}
    if ok:
        row["last_success"] = {**stamp, "level": level}
        if level == "confirmed":
            row["last_confirmed"] = stamp
    else:
        row["last_failure"] = stamp
    _evidence_path().write_text(json.dumps(ev, indent=1), encoding="utf-8")
    return row


def confirm_channel(channel, how="the user said they received it", now=None):
    return record_evidence(channel, True, how, level="confirmed", now=now)


def readiness(channel, now=None):
    """{"state": verified | failing | not verified, "line": ..., "last_success": ..., "last_failure": ...}."""
    row = _load_evidence().get(channel, {})
    ok, bad, conf = row.get("last_success"), row.get("last_failure"), row.get("last_confirmed")
    proof = conf if channel in NEEDS_CONFIRMATION else ok
    when = lambda r: time.strftime("%m-%d %H:%M", time.localtime(r["time"]))  # noqa: E731
    if proof and (not bad or proof["time"] >= bad["time"]):
        state, line = "verified", f"verified {when(proof)}: {proof['how']}"
    elif bad and (not proof or bad["time"] > proof["time"]):
        state, line = "failing", f"failing since {when(bad)}: {bad['how']}"
    elif ok and channel in NEEDS_CONFIRMATION:
        state, line = "not verified", f"sent {when(ok)} but nobody has confirmed it arrived on the phone"
    else:
        state, line = "not verified", "never delivered end to end"
    return {"channel": channel, "state": state, "line": line, "last_success": ok, "last_failure": bad,
            "last_confirmed": conf}


def readiness_lines():
    """One line per channel for `waggle doctor` and the HQ note."""
    names = {"telegram_text": "Telegram text", "telegram_voice": "Telegram voice note", "ntfy": "Urgent push (ntfy)",
             "call": "Phone call"}
    out = []
    for ch in CHANNELS:
        r = readiness(ch)
        line = f"{names[ch]}: {r['state']} ({r['line']})"
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


# ---- outbox ------------------------------------------------------------------------------------------

def _db():
    con = sqlite3.connect(data_path("outbox.db"), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY, created REAL, kind TEXT, text TEXT, path TEXT, title TEXT, urgent INTEGER,
        asked INTEGER DEFAULT 0, origin TEXT, source TEXT, topic TEXT, decision_id TEXT, not_before REAL,
        expires REAL, attempts INTEGER DEFAULT 0, next_try REAL, status TEXT, lease_until REAL, sent_at REAL,
        detail TEXT, held INTEGER DEFAULT 0, responded_at REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, next_try)")
    return con


def deliver(kind, text="", *, path="", title="", urgent=False, asked=False, origin="manual", source=None, topic="",
            decision_id=None, expires=None, settings=None, now=None, flush_now=True):
    """Queue one outbound message and try to send it right away. Returns the outbox row (dict).
    asked: the user asked for this (a reminder, "call me at 7"): it bypasses the daily cap like urgent items do."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    s = settings or Settings.load()
    now = now or time.time()
    oid = uuid.uuid4().hex[:12]
    status, held, not_before = "queued", 0, now
    if is_dev_source(source, s):
        status = "suppressed"
    elif not urgent and not asked and kind != "push" and in_quiet_hours(s, now):
        held, not_before = 1, quiet_ends(s, now)
    exp = expires or (max(not_before, now) + s.outbox_max_age_h * 3600)
    con = _db()
    con.execute("INSERT INTO outbox (id, created, kind, text, path, title, urgent, asked, origin, source, topic, "
                "decision_id, not_before, expires, next_try, status, held, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, now, kind, text or "", str(path or ""), title or "", int(bool(urgent)), int(bool(asked)),
                 origin, source or "", topic or "", decision_id, not_before, exp, not_before, status, held,
                 "developer/test activity: not sent to the phone" if status == "suppressed" else ""))
    con.commit()
    con.close()
    from . import events
    events.log("outbox", id=oid, msg_kind=kind, origin=origin, status=status, urgent=bool(urgent), held=bool(held),
               text=(text or "")[:120])
    if flush_now and status == "queued" and not held:
        flush(s, now, only=oid)
    return get(oid)


def get(oid):
    con = _db()
    r = con.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone()
    con.close()
    return dict(r) if r else None


def rows(since=0.0, status=None, origin=None):
    con = _db()
    q, args = "SELECT * FROM outbox WHERE created>=?", [since]
    if status:
        q += " AND status=?"
        args.append(status)
    if origin:
        q += f" AND origin IN ({','.join('?' * len(origin))})"
        args += list(origin)
    out = [dict(r) for r in con.execute(q + " ORDER BY created", args)]
    con.close()
    return out


def _claim(s, now, only=None):
    """Atomically take the due rows (lease them) so two flushers never send the same message."""
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        expired = [dict(r) for r in con.execute(
            "SELECT * FROM outbox WHERE status IN ('queued','retry') AND expires<?", (now,))]
        for r in expired:
            con.execute("UPDATE outbox SET status='expired', detail=? WHERE id=?",
                        ((r["detail"] or "not delivered before it expired")[:300], r["id"]))
        q = ("SELECT * FROM outbox WHERE (status IN ('queued','retry') OR (status='sending' AND lease_until<?)) "
             "AND next_try<=? AND not_before<=?")
        args = [now, now, now]
        if only:
            q += " AND id=?"
            args.append(only)
        due = [dict(r) for r in con.execute(q + " ORDER BY urgent DESC, created", args)]
        quiet = in_quiet_hours(s, now)
        claimed = []
        for r in due:
            if quiet and not r["urgent"] and not r["asked"] and r["kind"] != "push":
                ends = quiet_ends(s, now)  # a retry that drifted into the night waits for the morning
                con.execute("UPDATE outbox SET not_before=?, next_try=?, held=1 WHERE id=?", (ends, ends, r["id"]))
                continue
            con.execute("UPDATE outbox SET status='sending', lease_until=? WHERE id=?", (now + LEASE_S, r["id"]))
            claimed.append(r)
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    if expired:
        from . import events
        for r in expired:
            events.log("delivery", id=r["id"], msg_kind=r["kind"], origin=r["origin"], ok=False, status="expired",
                       why=r["detail"] or "expired")
    return claimed


def _finish(r, ok, detail, now):
    con = _db()
    if ok:
        con.execute("UPDATE outbox SET status='sent', sent_at=?, detail=?, attempts=attempts+1, lease_until=NULL "
                    "WHERE id=?", (now, detail[:300], r["id"]))
    else:
        n = (r["attempts"] or 0) + 1
        final = r["kind"] == "call"  # a call is not retried: the fallback (push + voice note) goes out instead
        con.execute("UPDATE outbox SET status=?, attempts=?, next_try=?, detail=?, lease_until=NULL WHERE id=?",
                    ("failed" if final else "retry", n, now + BACKOFF[min(n - 1, len(BACKOFF) - 1)], detail[:300],
                     r["id"]))
    con.commit()
    con.close()
    from . import events
    events.log("delivery", id=r["id"], msg_kind=r["kind"], origin=r["origin"], ok=ok, why=detail[:200],
               attempt=(r["attempts"] or 0) + 1)


def _merge_held(rows_):
    """Texts held over quiet hours go out as one morning message instead of a burst."""
    held = [r for r in rows_ if r["held"] and r["kind"] == "text"]
    if len(held) < 2:
        return rows_, []
    body = "While you were asleep:\n" + "\n".join("• " + r["text"].lstrip("🐝 ")[:600] for r in held)
    lead = {**held[0], "text": body[:3500]}
    rest = [r for r in rows_ if r not in held]
    return [lead] + rest, held[1:]


def flush(settings=None, now=None, only=None, transport=None):
    """Send everything that is due. Returns [(id, ok, detail)]. Safe to run from several processes at once."""
    s = settings or Settings.load()
    now = now or time.time()
    t = transport or get_transport()
    claimed = _claim(s, now, only)
    claimed, merged = _merge_held(claimed)
    out = []
    for r in claimed:
        try:
            if r["kind"] == "text":
                ok, detail = t.send_text(r["text"], s)
            elif r["kind"] == "voice":
                ok, detail = t.send_voice(r["path"], r["text"], s)
                if not ok and r["text"] and (r["attempts"] or 0) >= 2:  # voice keeps failing: send the words
                    ok2, d2 = t.send_text(r["text"], s)
                    ok, detail = ok2, f"voice failed ({detail}); sent as text: {d2}"
            elif r["kind"] == "file":
                ok, detail = t.send_file(r["path"], r["text"], s)
            elif r["kind"] == "push":
                ok, detail = t.push(r["text"], r["title"] or "Waggle", 5 if r["urgent"] else 3, s)
            else:
                ok, detail = t.call(r["text"], s)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        _finish(r, ok, detail, now)
        if r["kind"] != "call":  # a call's evidence comes from Twilio's final status (call.check_pending)
            record_evidence(CHANNEL[r["kind"]], ok, detail, ref=r["id"], now=now)
        if r["kind"] == "call" and not ok:
            _call_fallback(r, detail, s, now)
        out.append((r["id"], ok, detail))
    for m in merged:
        lead_ok = out[0][1] if out else False
        _finish(m, lead_ok, "sent inside the morning summary" if lead_ok else "morning summary failed", now)
        out.append((m["id"], lead_ok, "merged"))
    return out


def _call_fallback(r, why, s, now):
    """A call was wanted but can't happen: an urgent push plus a voice note (or text) instead."""
    text = r["text"]
    deliver("push", text, title="Waggle tried to call you", urgent=True, origin=r["origin"], topic=r["topic"],
            decision_id=r["decision_id"], settings=s, now=now)
    voice(f"I tried to call you. {text}", settings=s, urgent=True, origin=r["origin"], topic=r["topic"],
          decision_id=r["decision_id"], caption=f"📞 Call not possible ({why[:120]}). {text}", now=now)


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
          now=None, synth=None, source=None):
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
              source=source)
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
        row["phoned"] = r["status"] == "sent"
        row["outbox"] = r["id"]
        row["held"] = bool(r["held"])
    if (urgent if push is None else push) and not dev:
        r = deliver("push", text, title="🚨 Waggle needs you", urgent=True, origin=origin, settings=s)
        row["pushed"] = r["status"] == "sent"
    with open(data_path("notifications.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    from . import events
    events.log("notify", text=text[:200], urgent=urgent, quiet=quiet, phoned=row.get("phoned", False),
               pushed=row.get("pushed", False), dev=dev, origin=origin)
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


# ---- did the user answer? -------------------------------------------------------------------------------

def track_responses(settings=None, now=None, window_s=6 * 3600, turns=None):
    """Mark sent outreach the user answered (a Telegram message within window_s after it) and record that as
    confirmed evidence for the channel. turns = [(time, platform)] for tests; default from traces.db."""
    s = settings or Settings.load()
    now = now or time.time()
    con = _db()
    open_ = [dict(r) for r in con.execute(
        "SELECT * FROM outbox WHERE status='sent' AND responded_at IS NULL AND sent_at>=?", (now - 2 * 86400,))]
    con.close()
    if not open_:
        return 0
    if turns is None:
        try:
            from . import traces
            turns = [(r["time"], r.get("platform") or "") for r in traces.all_traces(since=now - 3 * 86400, limit=500)]
        except Exception:  # noqa: BLE001
            turns = []
    user_times = sorted(t for t, p in turns if p in s.followup_platforms)
    marked = 0
    con = _db()
    for r in open_:
        hit = next((t for t in user_times if r["sent_at"] < t <= r["sent_at"] + window_s), None)
        if hit:
            con.execute("UPDATE outbox SET responded_at=? WHERE id=?", (hit, r["id"]))
            marked += 1
            if r["kind"] in ("text", "voice"):
                record_evidence(CHANNEL[r["kind"]], True, "the user replied after it", ref=r["id"],
                                level="confirmed", now=hit)
    con.commit()
    con.close()
    return marked


def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args[:1] == ["confirm"] and len(args) > 1:
        print(json.dumps(confirm_channel(args[1], " ".join(args[2:]) or "the user said they received it")))
    elif args[:1] == ["flush"]:
        print(json.dumps(flush()))
    else:
        print("\n".join(readiness_lines()))
    return 0


if __name__ == "__main__":
    main()
