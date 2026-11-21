"""The daily end-to-end check ("canary"): does Waggle really answer the owner on Telegram, today?

The core runs one job (register_core, listed in waggle.core.plugins.MODULES):
  canary.run   cron 12:30 local (outside quiet hours), catch-up "latest" within 9 h (the PC is off at night: a
               missed run happens once when it starts, never twice). Detached, because the check takes minutes.
It starts tools/testclient/canary.py in the test client's own venv (%USERPROFILE%\\.waggle-testclient\\venv: it
logs in as the owner's real Telegram account) as a no-window child with a timeout, reads the one JSON line it
prints and records the result:
  capability ledger   `canary.roundtrip` and `canary.reminder` (waggle.core.ledger; health shows them as ready or
                      not, and a success only counts for 36 h, so a check that stops running turns red by itself)
  event               `canary.ok` / `canary.failed`
  meta                `canary.last` (the whole result, for `python -m waggle.canary status`)
After a passing round trip a third check, `context`, asks whether the chat turn it caused carried the injected
context block (context_alive.py, "hooks alive"; ledger `canary.context`); it is left out when no turn was recorded.
When a check fails, or cannot run (no venv, no saved login, the script crashed, it ran too long), the owner gets one
plain-words text AND one ntfy push (the phone alarm does not depend on Telegram), at most once a day for the same
failure. Nothing is sent when the check passes.

  python -m waggle.canary run [--no-reminder] [--no-alert]   run it now (after a deploy: --no-reminder)
  python -m waggle.canary status                              the last result and the ledger entries

Only the standard library is used here; Telegram is reached only by the child."""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import events
from .config import Settings

KIND = "canary.run"
TIMER = "canary.daily"
CRON = "30 12 * * *"
MAX_LATE_S = 9 * 3600
JOB_TIMEOUT_S = 600.0
LEASE_S = 120.0
LEDGER_TTL_S = 36 * 3600
PONG_WAIT_S = 90
REMINDER_WAIT_S = 150
SETUP_S = 90  # connecting, logging in, deleting the messages: headroom on top of the two waits
ALERT_ORIGIN = "canary-alert"
LAST_KEY = "canary.last"
ALERT_KEY = "canary.alert"
LEDGER = {"roundtrip": "canary.roundtrip", "reminder": "canary.reminder", "context": "canary.context"}
METHOD = "daily Telegram round trip through the test client"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash when run under pythonw
SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "testclient" / "canary.py"


# ---- the test client's files --------------------------------------------------------------------

def testclient_dir():
    """Same place as tools/testclient/tclib.state_dir()."""
    return Path(os.environ.get("WAGGLE_TESTCLIENT_DIR") or (Path.home() / ".waggle-testclient"))


def venv_python(base=None):
    base = Path(base) if base else testclient_dir()
    return base / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_problem(base=None, script=None, python=None):
    """Why the check cannot run at all ("" when it can): the test venv, the script or the saved login is missing."""
    base = Path(base) if base else testclient_dir()
    py, script = Path(python) if python else venv_python(base), Path(script) if script else SCRIPT
    if not py.exists():
        return (f"the test client's Python is missing ({py}); run tools\\testclient\\setup.ps1 and then "
                "login.py")
    if not script.exists():
        return f"the check script is missing ({script})"
    if not (base / "owner.session").exists() or not (base / "api.json").exists():
        return f"the test client has no saved Telegram login in {base}; run tools\\testclient\\login.py"
    return ""


# ---- running the child --------------------------------------------------------------------------

def child_timeout(reminder):
    return float(SETUP_S + PONG_WAIT_S + (REMINDER_WAIT_S if reminder else 0))


def spawn(python, script, args, timeout):
    """Run the script with no window and a timeout. Returns (returncode or None, stdout, stderr, timed_out). A
    child past the timeout is killed together with its children (the venv's python.exe starts the real one)."""
    from waggle.core import procs
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen([str(python), str(script), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
                         creationflags=procs.NO_WINDOW, startupinfo=procs.quiet_startupinfo())
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        try:
            procs.kill_tree(p.pid)
        except Exception:  # noqa: BLE001
            pass
        p.kill()
        try:
            out, err = p.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            out, err = "", ""
        return None, out or "", err or "", True


def parse_result(stdout):
    """The child's one JSON line: the last line that is an object with "ok" and a list of "checks", else None."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "ok" in data and isinstance(data.get("checks"), list):
            return data
    return None


def _clean_check(c):
    return {"name": str(c.get("name") or "check")[:40], "ok": bool(c.get("ok")),
            "seconds": float(c.get("seconds") or 0), "detail": str(c.get("detail") or "")[:300]}


def _failure(name, detail, seconds=0.0):
    return {"ok": False, "checks": [{"name": name, "ok": False, "seconds": seconds, "detail": detail[:300]}],
            "error": name}


def execute(reminder, *, base=None, script=None, python=None, timeout=None):
    """Run the child (or find out that it cannot run) and return the normalised result dict."""
    base = Path(base) if base else testclient_dir()
    py = Path(python) if python else venv_python(base)
    script = Path(script) if script else SCRIPT
    problem = setup_problem(base, script, py)
    if problem:
        return _failure("setup", f"The daily check couldn't run: {problem}.")
    timeout = float(timeout if timeout is not None else child_timeout(reminder))
    t0 = time.monotonic()
    code, out, err, timed_out = spawn(py, script, [] if reminder else ["--no-reminder"], timeout)
    took = round(time.monotonic() - t0, 1)
    if timed_out:
        return _failure("timeout", f"The daily check didn't finish within {timeout:.0f} s and was stopped.", took)
    data = parse_result(out)
    if data is None:
        tail = " ".join((err or out or "").split())[-200:]
        return _failure("script", f"The daily check script ended (exit {code}) without a result."
                        + (f" It said: {tail}" if tail else ""), took)
    res = {"ok": bool(data["ok"]) and all(c.get("ok") for c in data["checks"]),
           "checks": [_clean_check(c) for c in data["checks"]], "error": str(data.get("error") or "")[:80]}
    if data.get("id"):
        res["id"] = str(data["id"])[:20]
    if data.get("cleaned") is not None:
        res["cleaned"] = data["cleaned"]
    return res


# ---- recording and alerting ---------------------------------------------------------------------

def record(result, *, reminder, now=None):
    """Ledger entries, the event and `canary.last`. A check that did not run leaves its ledger entry alone."""
    from waggle.core import events as core_events
    from waggle.core import ledger, store
    now = store.now_or(now)
    ref = "canary:" + result.get("id", "")
    for c in result["checks"]:
        name = LEDGER.get(c["name"])
        if name is None and not c["ok"]:
            name = LEDGER["roundtrip"]  # setup / timeout / script / telegram: the round trip did not work
        if name is not None:
            ledger.record(name, c["ok"], method=METHOD, evidence_ref=ref, ttl_s=LEDGER_TTL_S,
                          detail=c["detail"], now=now)
    core_events.emit("canary.ok" if result["ok"] else "canary.failed", source="canary", ref=ref, now=now,
                     data={"reminder": reminder, "checks": [{k: c[k] for k in ("name", "ok", "seconds")}
                                                            for c in result["checks"]]})
    store.set_meta(LAST_KEY, json.dumps({**result, "ts": now, "reminder": reminder}))


def failure_key(result):
    return "+".join(c["name"] for c in result["checks"] if not c["ok"]) or "unknown"


def alert_text(result):
    """What the owner reads: the failed checks' own plain sentences, behind "Daily check:"."""
    said = " ".join(_plain(c["detail"]) for c in result["checks"] if not c["ok"]) or "Something failed."
    return said if said.startswith("The daily check") else "Daily check: " + said


def _plain(detail):
    d = detail.strip()
    return d[:1].upper() + d[1:] if d else "Something failed."


def alert_due(result, now):
    """True the first time today that this failure is seen."""
    from waggle.core import store
    day = dt.date.fromtimestamp(now).isoformat()
    try:
        seen = json.loads(store.get_meta(ALERT_KEY) or "{}")
    except ValueError:
        seen = {}
    return not (seen.get("day") == day and failure_key(result) in (seen.get("keys") or []))


def _mark_alerted(result, now):
    from waggle.core import store
    day = dt.date.fromtimestamp(now).isoformat()
    try:
        seen = json.loads(store.get_meta(ALERT_KEY) or "{}")
    except ValueError:
        seen = {}
    keys = list(seen.get("keys") or []) if seen.get("day") == day else []
    store.set_meta(ALERT_KEY, json.dumps({"day": day, "keys": [*keys, failure_key(result)]}))


def alert(result, settings, now):
    """One text and one push to the owner, once a day per failure. Returns {"sent": bool, ...}."""
    from . import notify
    if not alert_due(result, now):
        return {"sent": False, "why": "already alerted today"}
    day = dt.date.fromtimestamp(now).isoformat()
    key = failure_key(result)
    text = alert_text(result)
    chats = [str(c).strip() for c in (getattr(settings, "engine_chats", None) or []) if str(c).strip()]
    ids = {}
    for kind, extra in (("text", {"chat": chats[0] if chats else ""}),
                        ("push", {"title": "Waggle daily check failed", "urgent": True})):
        row = notify.deliver(kind, text, origin=ALERT_ORIGIN, ref=f"{day}:{key}:{kind}", topic="canary",
                             settings=settings, now=now, **extra)
        ids[kind] = row.get("id")
    _mark_alerted(result, now)
    return {"sent": True, "text": ids["text"], "push": ids["push"]}


def add_context_check(result, since):
    """Hooks alive: the round trip just caused a chat turn, so a turn from after `since` must exist and carry the
    injected context block (context_alive.py). Added as a third check; a failure makes the whole result fail. Left
    out when there is nothing to judge (the round trip did not run, or no turn was recorded here)."""
    rt = next((c for c in result["checks"] if c["name"] == "roundtrip"), None)
    if rt is None or not rt["ok"]:
        return
    try:
        from . import context_alive
        r = context_alive.check(since=since - 5, min_turns=1, strict=True)
    except Exception as e:  # noqa: BLE001  (a broken check must not hide the round trip's own result)
        events.log("canary", action="context_check_failed", error=f"{type(e).__name__}: {e}"[:200])
        return
    if not r["evaluated"]:
        return
    ok = r["status"] == "ok"
    result["checks"].append({"name": "context", "ok": ok, "seconds": 0.0,
                             "detail": r["detail"] if ok else "The daily check found the chat's context block "
                             f"missing: {r['detail']}."})
    if not ok:
        result["ok"] = False


def run_check(settings=None, now=None, *, reminder=True, alert_owner=True, **spawn_args):
    """The whole daily check: run the child, record the result, alert on a failure. Returns the result dict plus
    "alert" (what was sent). `spawn_args` (base, script, python, timeout) are for tests."""
    s = settings or Settings.load()
    now = now or time.time()
    t_start = time.time()
    result = execute(reminder, **spawn_args)
    add_context_check(result, t_start)
    record(result, reminder=reminder, now=now)
    result["alert"] = {"sent": False, "why": "passed" if result["ok"] else "alerts off"}
    if not result["ok"] and alert_owner:
        try:
            result["alert"] = alert(result, s, now)
        except Exception as e:  # noqa: BLE001  (a notify problem must not hide the result)
            result["alert"] = {"sent": False, "why": f"{type(e).__name__}: {e}"[:200]}
            events.log("canary", action="alert_failed", error=result["alert"]["why"])
    return result


# ---- core job ------------------------------------------------------------------------------------

def canary_job(payload=None, *, settings=None, now=None, **spawn_args):
    """Job canary.run (detached). The daily timer's payload says reminder true; a post-deploy run passes false.
    A failed check is a result, not a job failure: a retry would send more test messages."""
    reminder = bool((payload or {}).get("reminder", True))
    res = run_check(settings, now, reminder=reminder, **spawn_args)
    return {"ok": res["ok"], "checks": [{k: c[k] for k in ("name", "ok", "seconds")} for c in res["checks"]],
            "alert": res["alert"]}


HANDLERS = {KIND: canary_job}


def timer_specs():
    """(timer id, kind, spec, job kind, catch_up, max_late_s, payload)."""
    return [(TIMER, "cron", CRON, KIND, "latest", MAX_LATE_S, {"reminder": True})]


def register_jobs():
    from waggle.core import jobs
    for kind, fn in HANDLERS.items():
        jobs.register(kind, fn, detached=True, max_attempts=1, lease_s=LEASE_S, timeout_s=JOB_TIMEOUT_S)
    return list(HANDLERS)


def register_core(now=None):
    """Called by the core process at every start (idempotent). Returns the job kinds and timer ids."""
    from waggle.core import timers
    report = {"jobs": register_jobs(), "timers": []}
    for tid, kind, spec, job_kind, policy, max_late, payload in timer_specs():
        timers.add(tid, kind, spec, job_kind, payload, catch_up=policy, max_late_s=max_late, origin="canary",
                   now=now)
        report["timers"].append(tid)
    return report


# ---- status and command line ---------------------------------------------------------------------

def status(now=None):
    """The last result plus the canary ledger entries."""
    from waggle.core import ledger, store
    try:
        last = json.loads(store.get_meta(LAST_KEY) or "null")
    except ValueError:
        last = None
    entries = [r for r in ledger.status(now) if r["name"] in LEDGER.values()]
    return {"last": last, "ledger": [{"name": r["name"], "ready": r["ready"], "reason": r["reason"],
                                      "verified_at": r["verified_at"], "last_failure_at": r["last_failure_at"],
                                      "last_failure": r["last_failure"]} for r in entries]}


def format_status(d):
    def when(ts):
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"
    last = d["last"]
    if not last:
        lines = ["Daily check: has not run yet"]
    else:
        lines = [f"Daily check: {'passed' if last['ok'] else 'FAILED'} at {when(last['ts'])}"
                 f"{'' if last.get('reminder') else ' (no reminder check)'}"]
        lines += [f"  {c['name']}: {'ok' if c['ok'] else 'FAILED'} in {c['seconds']} s"
                  f"{'' if c['ok'] else ' - ' + c['detail']}" for c in last["checks"]]
    for r in d["ledger"]:
        lines.append(f"  ledger {r['name']}: {'ready' if r['ready'] else 'NOT ready'} ({r['reason']})")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    cmd = argv[0] if argv else "status"
    if cmd == "run":
        res = run_check(reminder="--no-reminder" not in argv, alert_owner="--no-alert" not in argv)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    if cmd == "status":
        print(format_status(status()))
        return 0
    print("usage: python -m waggle.canary run [--no-reminder] [--no-alert] | status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
