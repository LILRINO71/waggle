"""Phone calls through Twilio (free trial credit; calls only reach your verified number).

Waggle calls you for verified critical alerts (never in quiet hours, at most `max_calls_per_day`) and when
you ask it to ("call me in ten minutes about X" -> the call_me tool). The message is read out in a British
voice and repeated once. No webhook or public server is needed: the script travels inline with the call.

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


def env_path():
    return Path.home() / ".waggle" / "twilio.env"


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


def configured():
    return creds() is not None


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
    return sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.strip()
               and json.loads(l).get("day") == today and json.loads(l).get("ok"))


def _post(c, data, opener=None):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{c['TWILIO_ACCOUNT_SID']}/Calls.json"
    auth = base64.b64encode(f"{c['TWILIO_ACCOUNT_SID']}:{c['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Authorization": f"Basic {auth}"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        return json.loads(r.read())


def call(message, reason="manual", settings=None, force=False, opener=None):
    """Place a call. Returns {"ok": bool, "why": str, "sid": str}. `force` skips the daily cap (explicit asks)."""
    s = settings or Settings.load()
    c = creds()
    if not c:
        out = {"ok": False, "why": "calls not set up (run Setup Calls.bat)"}
    elif not force and calls_today() >= s.max_calls_per_day:
        out = {"ok": False, "why": f"daily call limit ({s.max_calls_per_day}) reached"}
    else:
        try:
            res = _post(c, {"To": c["TWILIO_TO"], "From": c["TWILIO_FROM"], "Twiml": twiml(message)}, opener)
            out = {"ok": True, "why": "calling", "sid": res.get("sid", "")}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("message", "")
            except Exception:  # noqa: BLE001
                detail = ""
            out = {"ok": False, "why": f"Twilio error {e.code}: {detail}"[:200]}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "why": f"call failed: {e}"[:200]}
    with open(data_path("calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), "day": dt.date.today().isoformat(), "reason": reason,
                            "ok": out["ok"], "why": out["why"]}) + "\n")
    events.log("call", reason=reason, ok=out["ok"], why=out["why"])
    return out


# ---- "call me in 10 minutes" -------------------------------------------------------------------------------

CALL_ME = re.compile(r"\b(?:call|ring|phone) me\b(?:\s+(?:in|after)\s+(?:(\d+(?:\.\d+)?)|(a|an|one|half an?))\s*"
                     r"(second|sec|minute|min|hour|hr)s?)?(?:\s+(?:about|to|and|with|re:?)\s+(.+))?", re.I)


def parse_request(text):
    """'call me in 10 minutes about the build' -> (10.0, 'the build'); 'call me' -> (0, ''); else None."""
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
    """Place any scheduled calls that are due. Called every minute by the waggle-calls cron job."""
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
    if args[:1] == ["due"]:
        run_due()
        return 0
    print(json.dumps(call(" ".join(args) or "This is a test call. Calls are working.", reason="test", force=True)))
    return 0


if __name__ == "__main__":
    main()
