"""Phone calls through Twilio (free trial credit; calls only reach your verified number).

Waggle calls you for urgent things the reasoning step picks (never in quiet hours, at most `max_calls_per_day`) and
when you ask ("call me in ten minutes", "call me at 7 tomorrow"). The message is read out and repeated once. No
webhook or public server is needed: the script travels inline with the call.

Honest status: provisioned() asks Twilio (read-only) whether a call can really be placed: the account answers, it
owns the From number, and on a trial account your own number is a verified caller ID. The answer is cached.
configured() is that answer, so "four strings in a file" never reads as ready. Whether calls have actually worked
end to end comes from the evidence record in notify (a call Twilio reports as completed). When a call is wanted
but impossible, notify sends an urgent push plus a voice note instead.

Credentials live in ~/.waggle/twilio.env, which you fill in yourself (Setup Calls.bat walks you through it):
    TWILIO_ACCOUNT_SID=AC...
    TWILIO_AUTH_TOKEN=...
    TWILIO_FROM=+1...   your Twilio number
    TWILIO_TO=+1...     your own (verified) phone
"""
import base64
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

from . import events
from .config import Settings, data_path

KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO")
VOICE = "Polly.Brian"
API = "https://api.twilio.com/2010-04-01"
CACHE_OK_S = 6 * 3600
CACHE_BAD_S = 30 * 60
STEP_FROM = ("Get the free trial phone number in the Twilio Console (Phone Numbers > Manage > Buy a number; the trial "
             "credit covers it) and put it in TWILIO_FROM in ~/.waggle/twilio.env")
STEP_TO = ("Verify your own phone in the Twilio Console (Phone Numbers > Manage > Verified Caller IDs) and put that "
           "number in TWILIO_TO in ~/.waggle/twilio.env")


def env_path():
    return data_path("twilio.env")


def creds(path=None):
    """The four Twilio values, or None if any is missing."""
    p = Path(path or env_path())
    if not p.exists():
        return None
    vals = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            vals[m.group(1)] = m.group(2).strip('"\'')
    return vals if all(vals.get(k) for k in KEYS) else None


# ---- honest status -----------------------------------------------------------------------------------

def _get(c, path, opener=None):
    auth = base64.b64encode(f"{c['TWILIO_ACCOUNT_SID']}:{c['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req = urllib.request.Request(f"{API}/Accounts/{c['TWILIO_ACCOUNT_SID']}{path}",
                                 headers={"Authorization": f"Basic {auth}"})
    with (opener or urllib.request.urlopen)(req, timeout=15) as r:
        return json.loads(r.read())


def _status_file():
    return data_path("call_status.json")


def _load_status():
    try:
        return json.loads(_status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_status(st):
    _status_file().write_text(json.dumps(st, indent=1), encoding="utf-8")


def check_twilio(c, opener=None):
    """Read-only checks. Returns {"ready", "missing", "facts"}; never places or buys anything."""
    facts = {}
    try:
        acct = _get(c, ".json", opener)
    except urllib.error.HTTPError as e:
        return {"ready": False, "missing": f"Twilio refused the account credentials (HTTP {e.code}); re-copy the "
                                           "Account SID and Auth Token into ~/.waggle/twilio.env", "facts": facts}
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "missing": f"Twilio not reachable right now ({type(e).__name__}); will retry",
                "facts": facts, "transient": True}
    facts.update(status=acct.get("status"), type=acct.get("type"))
    if acct.get("status") != "active":
        return {"ready": False, "missing": f"the Twilio account is {acct.get('status')}; reactivate it in the Console",
                "facts": facts}
    q = urllib.parse.urlencode({"PhoneNumber": c["TWILIO_FROM"]})
    try:
        owned = _get(c, f"/IncomingPhoneNumbers.json?{q}", opener).get("incoming_phone_numbers") or []
        facts["owns_from"] = bool(owned)
        if not owned:
            return {"ready": False, "missing": STEP_FROM, "facts": facts}
        if (acct.get("type") or "").lower() == "trial":
            q = urllib.parse.urlencode({"PhoneNumber": c["TWILIO_TO"]})
            ids = _get(c, f"/OutgoingCallerIds.json?{q}", opener).get("outgoing_caller_ids") or []
            facts["to_verified"] = bool(ids)
            if not ids:
                return {"ready": False, "missing": STEP_TO, "facts": facts}
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "missing": f"Twilio check failed ({type(e).__name__}); will retry", "facts": facts,
                "transient": True}
    return {"ready": True, "missing": "", "facts": facts}


def provisioned(refresh=False, opener=None, now=None):
    """Can a call be placed at all? Cached (6 h when ready, 30 min otherwise). No credentials -> no network."""
    now = now or time.time()
    c = creds()
    if not c:
        return {"ready": False, "missing": "calls are not set up: run Setup Calls.bat (free Twilio trial)",
                "checked": now, "facts": {}}
    st = _load_status()
    cached = st.get("provisioned")
    key = f"{c['TWILIO_ACCOUNT_SID']}|{c['TWILIO_FROM']}|{c['TWILIO_TO']}"
    if cached and not refresh and cached.get("key") == key and \
            now - cached.get("checked", 0) < (CACHE_OK_S if cached.get("ready") else CACHE_BAD_S):
        return cached
    res = {**check_twilio(c, opener), "checked": now, "key": key}
    st["provisioned"] = res
    _save_status(st)
    events.log("call_status", ready=res["ready"], missing=res["missing"][:160])
    return res


def configured():
    """True only when Twilio says a call can be placed (cached). See provisioned() for the missing step."""
    try:
        return bool(provisioned()["ready"])
    except Exception:  # noqa: BLE001
        return False


def status_line():
    """For doctor/HQ: readiness from evidence, plus the exact missing step when calls can't happen."""
    from . import notify
    r = notify.readiness("call")
    p = provisioned()
    if not p["ready"]:
        return f"Phone calls: not possible. Next step: {p['missing']}"
    return f"Phone calls: {r['state']} ({r['line']})"


# ---- placing calls -----------------------------------------------------------------------------------

def twiml(message):
    msg = escape(" ".join((message or "").split())[:600])
    return (f'<Response><Say voice="{VOICE}">Hello, this is Waggle. {msg}</Say><Pause length="1"/>'
            f'<Say voice="{VOICE}">Again: {msg}</Say><Say voice="{VOICE}">Details are on Telegram. Goodbye.</Say>'
            "</Response>")


def calls_today():
    p = data_path("calls.jsonl")
    if not p.exists():
        return 0
    today = dt.date.today().isoformat()
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            n += r.get("day") == today and bool(r.get("ok"))
    return n


def _post(c, data, opener=None):
    url = f"{API}/Accounts/{c['TWILIO_ACCOUNT_SID']}/Calls.json"
    auth = base64.b64encode(f"{c['TWILIO_ACCOUNT_SID']}:{c['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Authorization": f"Basic {auth}"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        return json.loads(r.read())


def place(message, reason="manual", settings=None, opener=None):
    """The raw Twilio request (the transport's call). Returns {"ok", "why", "sid"}; logs calls.jsonl."""
    c = creds()
    if not c:
        out = {"ok": False, "why": "calls not set up (run Setup Calls.bat)"}
    else:
        try:
            res = _post(c, {"To": c["TWILIO_TO"], "From": c["TWILIO_FROM"], "Twiml": twiml(message)}, opener)
            out = {"ok": True, "why": "calling", "sid": res.get("sid", "")}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("message", "")
            except Exception:  # noqa: BLE001
                detail = ""
            out = {"ok": False, "why": f"Twilio error {e.code}: {detail}"[:200], "code": e.code,
                   "permanent": 400 <= e.code < 500 and e.code != 429}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "why": f"call failed: {e}"[:200]}
    with open(data_path("calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "day": dt.date.today().isoformat(), "reason": reason,
                            "ok": out["ok"], "why": out["why"]}) + "\n")
    events.log("call", reason=reason, ok=out["ok"], why=out["why"])
    if out["ok"] and out.get("sid"):
        st = _load_status()
        st.setdefault("pending", []).append({"sid": out["sid"], "time": time.time()})
        _save_status(st)
    return out


def call(message, reason="manual", settings=None, force=False, opener=None):
    """Place a call. Returns {"ok": bool, "why": str, "sid": str}. `force` = the user asked (skips the daily cap).
    Goes through notify's outbox, so a call that can't happen turns into an urgent push + voice note."""
    s = settings or Settings.load()
    if not force and calls_today() >= s.max_calls_per_day:
        return {"ok": False, "why": f"daily call limit ({s.max_calls_per_day}) reached"}
    if opener is not None:  # direct (tests, diagnostics)
        return place(message, reason, s, opener)
    from . import notify
    row = notify.deliver("call", message, urgent=True, asked=force, origin=reason, settings=s)
    ok = row["status"] == "queued"
    return {"ok": ok, "why": "queued: the core places the call" if ok else f"call not queued ({row['status']})",
            "outbox": row["id"]}


CALL_MAX_LATE_S = 15 * 60  # a call that could not ring within this long becomes a text: the moment has passed
CALL_MAX_ATTEMPTS = 3


def _fallback(row, why, *, late=False):
    """A call was wanted but cannot ring. Late (the PC was off): a text that says so. Otherwise: an urgent push
    plus a text. Idempotent per outbox row, so a retried sender never doubles them."""
    from . import notify
    text = row.get("body") or ""
    meta = row.get("meta") or {}
    origin, chat, rid = row.get("origin") or "call", row.get("chat") or "", row.get("id")
    kw = dict(origin=origin, chat=chat, topic=meta.get("topic", ""), decision_id=meta.get("decision_id"))
    if late:
        at = dt.datetime.fromtimestamp(meta.get("scheduled_ts") or row.get("created") or time.time())
        return [notify.deliver("text", f"📞 late: the PC was off, so I didn't call at {at:%H:%M}. {text}".strip(),
                               asked=True, ref=f"callfb:{rid}:late", **kw)]
    return [notify.deliver("push", text, title="Waggle tried to call you", urgent=True, ref=f"callfb:{rid}:push",
                           **kw),
            notify.deliver("text", f"📞 Call not possible ({why[:120]}). {text}".strip(), urgent=True,
                           ref=f"callfb:{rid}:text", **kw)]


def call_sender(row, *, opener=None, now=None):
    """The core sender's transport for kind "call": returns Twilio's call sid or raises notify.SendError.

    Too late (more than CALL_MAX_LATE_S after it was due) -> a text saying so, no ring. Not provisioned -> an
    urgent push + text instead. A refused request, or the last attempt failing -> the same fallback. A placed call
    is only the fact "call.placed"; "call" becomes ready when Twilio later reports the call completed
    (check_pending)."""
    from . import notify
    now = time.time() if now is None else float(now)
    meta = row.get("meta") or {}
    due = meta.get("scheduled_ts") or row.get("created") or now
    if now - due > CALL_MAX_LATE_S:
        _fallback(row, "too late", late=True)
        raise notify.SendError(f"call {int((now - due) / 60)} min late: sent as a text instead", permanent=True)
    p = provisioned(opener=opener, now=now)
    if not p["ready"]:
        _fallback(row, p["missing"])
        raise notify.SendError(f"calls not possible: {p['missing']}"[:300], permanent=True)
    res = place(row.get("body") or "", reason=row.get("origin") or "call", opener=opener)
    if res["ok"] and res.get("sid"):
        notify.record_evidence("call.placed", True, "Twilio accepted the call request", ref=res["sid"], now=now)
        return res["sid"]
    notify.record_evidence("call", False, res["why"], ref=f"outbox:{row.get('id')}", now=now)
    if res.get("permanent") or (row.get("attempts") or 1) >= CALL_MAX_ATTEMPTS:
        _fallback(row, res["why"])
        raise notify.SendError(res["why"], permanent=True)
    raise notify.SendError(res["why"], retry_after=30)


def check_pending(opener=None, now=None):
    """Ask Twilio (read-only) how placed calls ended; a completed call is the evidence that calls work."""
    from . import notify
    st = _load_status()
    pending = st.get("pending") or []
    c = creds()
    if not pending or not c:
        return []
    now = now or time.time()
    keep, done = [], []
    for p in pending:
        try:
            res = _get(c, f"/Calls/{p['sid']}.json", opener)
        except Exception:  # noqa: BLE001
            if now - p["time"] < 86400:
                keep.append(p)
            continue
        status = res.get("status")
        if status in ("queued", "ringing", "in-progress", "initiated"):
            keep.append(p)
            continue
        ok = status == "completed"
        how = f"Twilio call {status}" + (f", {res.get('duration')} s" if res.get("duration") else "")
        notify.record_evidence("call", ok, how, ref=p["sid"], now=now)
        done.append((p["sid"], status))
    st["pending"] = keep
    _save_status(st)
    return done


# ---- "call me in 10 minutes" / "call me at 7 tomorrow" ---------------------------------------------------

CALL_ME = re.compile(r"\b(?:call|ring|phone) me\b(?:\s+(?:in|after)\s+(?:(\d+(?:\.\d+)?)|(a|an|one|half an?))\s*"
                     r"(second|sec|minute|min|hour|hr)s?)?(?:\s+(?:about|to|and|with|re:?)\s+(.+))?", re.I)
CALL_AT = re.compile(r"\b(?:call|ring|phone) me\b\s+((?:(?:at|tomorrow|tonight|today|on|this|next)\b[^.!?]*?))"
                     r"(?:\s+(?:about|to|and|with|re:?)\s+(.+))?$", re.I)


def parse_request(text, now=None):
    """'call me in 10 minutes about the build' -> (10.0, 'the build'); 'call me' -> (0, '');
    'call me at 7 tomorrow about the meet' -> (minutes until then, 'the meet'); else None."""
    m = CALL_AT.search((text or "").strip(" .!?"))
    if m:
        from . import when
        nowd = dt.datetime.fromtimestamp(now) if isinstance(now, (int, float)) else now or dt.datetime.now()
        at, has_time = when.parse(m.group(1), nowd)
        if at and has_time and at > nowd:
            return round((at - nowd).total_seconds() / 60, 1), (m.group(2) or "").strip(" .!?")
    m = CALL_ME.search(text or "")
    if not m:
        return None
    num, word, unit, about = m.groups()
    n = float(num) if num else (0.5 if word and word.lower().startswith("half") else 1.0 if word else 0.0)
    minutes = n * 60 if unit and unit.lower().startswith("h") else n / 60 if unit and unit.lower().startswith("s") else n
    return minutes, (about or "").strip(" .!?")


def schedule(message, minutes):
    p = data_path("scheduled_calls.json")
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    due = time.time() + max(0, float(minutes)) * 60
    rows.append({"due": due, "message": message})
    p.write_text(json.dumps(rows), encoding="utf-8")
    return due


def run_due(settings=None, now=None, caller=None):
    """Place any scheduled calls that are due (the minute tick calls this)."""
    p = data_path("scheduled_calls.json")
    if not p.exists():
        return []
    rows = json.loads(p.read_text(encoding="utf-8"))
    now = now or time.time()
    due = [r for r in rows if r["due"] <= now]
    if not due:
        return []
    p.write_text(json.dumps([r for r in rows if r["due"] > now]), encoding="utf-8")
    return [(caller or call)(r["message"], reason="scheduled", settings=settings, force=True) for r in due]


def main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["due"]:  # old cron entry: the minute tick now does this and more
        from . import tick
        tick.main([])
        return 0
    if args[:1] == ["status"]:
        print(json.dumps(provisioned(refresh=True), indent=1))
        return 0
    print(json.dumps(call(" ".join(args) or "This is a test call. Calls are working.", reason="test", force=True)))
    return 0


if __name__ == "__main__":
    main()
