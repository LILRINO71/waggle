"""A cheap gate in front of Claude for work that runs on its own (borrow-list row 3).

Background work that would wake Claude (outreach decisions, the autonomous work loop's "pick a step", routines)
can carry a pre-check that costs nothing: a Python function, or one yes/no question to the local model. The check
answers {"wake": bool, "p_needed": 0..1, "p_urgent": 0..1, "data": anything}. evaluate() turns that into one of
four outcomes and logs it:

  RUN     the work goes ahead as it always did
  SKIP    the check is very sure it is not needed: P(not needed) >= SKIP_P (0.95) AND it said wake=False.
          Anything less sure, and every failure, runs. The caller keeps whatever it was going to look at (for
          example, the signals go to the evening note), so a skip never loses work.
  HOLD    quiet hours, work that would reach the owner (quiet_hold=True), and P(urgent) < URGENT_P (0.80): held
          until the quiet period ends (verdict.until)
  BYPASS  no check, the gates are switched off, or the check timed out (TIMEOUT_S) or failed: the old path

A skip needs both halves because the thresholds are lopsided on purpose: a gate that is too eager hides things, and
this one only ever acts when it is nearly certain. Basic duties (reminders, mail, calendar, delivery) never carry
a gate. The idea (a check before a model run, lopsided thresholds, a quiet-hours hold, RUN/SKIP/HOLD/BYPASS with a
reason) comes from nanoclaw's pre-task scripts and GAIA's trigger gates; this is a fresh implementation.

Every outcome is an event of kind "gate", so counts() and evening_line() can say "the gates skipped N Claude calls".
Switch everything off with config.json "gates_enabled": false. Other places can plug a check in without editing
their caller: register("name", fn) and the caller asks registered("name").
"""
import json
import math
import threading
import time

from .config import Settings

RUN, SKIP, HOLD, BYPASS = "RUN", "SKIP", "HOLD", "BYPASS"
SKIP_P = 0.95        # skip only when P(not needed) is at least this
URGENT_P = 0.80      # in quiet hours, go ahead only when P(urgent) is at least this
TIMEOUT_S = 3.0      # a slower check is a failed check: the old path runs ("p95 measured at 370 ms")


class Verdict:
    def __init__(self, outcome, reason, *, p_needed=None, p_urgent=None, data=None, ms=0, until=None, name=""):
        self.outcome, self.reason, self.p_needed, self.p_urgent = outcome, reason, p_needed, p_urgent
        self.data, self.ms, self.until, self.name = data, ms, until, name

    @property
    def skip(self):
        return self.outcome == SKIP

    @property
    def hold(self):
        return self.outcome == HOLD

    @property
    def proceed(self):
        return self.outcome in (RUN, BYPASS)

    def as_dict(self):
        return {"outcome": self.outcome, "reason": self.reason, "p_needed": self.p_needed, "p_urgent": self.p_urgent,
                "ms": self.ms, "until": self.until, "gate": self.name}

    def __repr__(self):
        return f"<Verdict {self.outcome}: {self.reason}>"


# ---- plug-in points for places without a gate of their own yet -----------------------------------------------

_registry = {}


def register(name, fn):
    """Give a named place (for example "objective") a check: fn(**context) -> the answer dict (or bool)."""
    _registry[name] = fn


def registered(name):
    return _registry.get(name)


# ---- reading an answer ---------------------------------------------------------------------------------------

def _prob(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return min(1.0, max(0.0, float(v)))


def _answer(raw):
    """(wake, p_needed, p_urgent, data) from a check's answer; ValueError when it is not an answer at all.
    A bool is a certain answer. Without p_needed a wake=False answer is a certain 'not needed' (a script's verdict)
    and wake=True a certain 'needed'. Missing p_urgent counts as 0."""
    if isinstance(raw, bool):
        return raw, (1.0 if raw else 0.0), 0.0, None
    if not isinstance(raw, dict):
        raise ValueError(f"a gate answers a dict or a bool, not {type(raw).__name__}")
    p_needed, p_urgent = _prob(raw.get("p_needed")), _prob(raw.get("p_urgent"))
    wake = raw.get("wake")
    if not isinstance(wake, bool):
        if p_needed is None:
            raise ValueError("the answer has neither wake nor p_needed")
        wake = p_needed >= 0.5
    if p_needed is None:
        p_needed = 1.0 if wake else 0.0
    return wake, p_needed, (p_urgent or 0.0), raw.get("data")


def _timed(fn, timeout):
    """fn() with a wall-clock limit. A check that overruns is abandoned (its thread is a daemon)."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            box["error"] = e

    t = threading.Thread(target=target, name="waggle-gate", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "gates_enabled", True))


def evaluate(name, check, *, settings=None, now=None, quiet_hold=False, timeout=TIMEOUT_S, subject="", log=True):
    """Run `check` (a zero-argument function returning an answer, see the module doc) and decide.
    check None -> BYPASS without a log line (the place has no gate). quiet_hold: work that would reach the owner
    waits for the end of quiet hours unless P(urgent) is high. Never raises."""
    t0 = time.time()
    now = now or t0
    if check is None:
        return Verdict(BYPASS, "no gate", name=name)
    try:
        s = settings or Settings.load()
    except Exception:  # noqa: BLE001
        s = None
    if s is not None and not enabled(s):
        return _done(Verdict(BYPASS, "gates are switched off (gates_enabled)", name=name), subject, t0, log)
    try:
        wake, p_needed, p_urgent, data = _answer(_timed(check, timeout))
    except Exception as e:  # noqa: BLE001 - timeout or any failure: the old path, never lost work
        why = (f"timed out after {timeout:g} s" if isinstance(e, TimeoutError) else
               f"{type(e).__name__}: {e}"[:120])
        return _done(Verdict(BYPASS, f"gate failed ({why}): old path", name=name), subject, t0, log)
    p_not = 1.0 - p_needed
    if not wake and p_not >= SKIP_P:
        return _done(Verdict(SKIP, f"P(not needed) {p_not:.2f} >= {SKIP_P}" + _why(data), p_needed=p_needed,
                             p_urgent=p_urgent, data=data, name=name), subject, t0, log)
    if quiet_hold and s is not None:
        from . import notify
        if notify.in_quiet_hours(s, now) and p_urgent < URGENT_P:
            return _done(Verdict(HOLD, f"quiet hours and P(urgent) {p_urgent:.2f} < {URGENT_P}: held until morning",
                                 p_needed=p_needed, p_urgent=p_urgent, data=data, until=notify.quiet_ends(s, now),
                                 name=name), subject, t0, log)
    reason = ("P(not needed) %.2f is below %s: runs" % (p_not, SKIP_P)) if not wake else "needed"
    return _done(Verdict(RUN, reason + _why(data), p_needed=p_needed, p_urgent=p_urgent, data=data, name=name),
                 subject, t0, log)


def _why(data):
    if isinstance(data, dict) and data.get("why"):
        return f" ({str(data['why'])[:100]})"
    if isinstance(data, str) and data:
        return f" ({data[:100]})"
    return ""


def _done(v, subject, t0, log):
    v.ms = int((time.time() - t0) * 1000)
    if log:
        try:
            from . import events
            events.log("gate", gate=v.name, outcome=v.outcome, reason=v.reason[:200], subject=str(subject)[:80],
                       p_needed=v.p_needed, p_urgent=v.p_urgent, ms=v.ms)
        except Exception:  # noqa: BLE001 - logging never breaks the work
            pass
    return v


# ---- a question for the local model --------------------------------------------------------------------------

LOCAL_SYSTEM = """You are a cheap pre-check for a personal assistant. A stronger and more expensive model will run
only if your answer says it is worth it. Read the question and the facts, then answer JSON only:
{"wake": true or false, "p_needed": 0..1, "p_urgent": 0..1, "why": "one short sentence"}
wake=true means the expensive model should look at this. p_needed is how sure you are that it is needed (1 = surely,
0 = surely not); p_urgent is how sure you are that it cannot wait until tomorrow morning. When in doubt, wake=true."""


LOCAL_SCHEMA = {"type": "object", "required": ["wake", "p_needed"], "properties": {
    "wake": {"type": "boolean"}, "p_needed": {"type": "number"}, "p_urgent": {"type": "number"},
    "why": {"type": "string"}}}


def _local_ask(messages, settings, timeout):
    from . import ollama
    return ollama.chat_json(messages, schema=LOCAL_SCHEMA, settings=settings, timeout=timeout)


def local_check(question, facts="", *, settings=None, ask=None, timeout=TIMEOUT_S):
    """A check that asks the local model one question. ask(messages, settings, timeout) -> dict is a seam for tests.
    Pass the result to evaluate(). The model's own numbers are only ever trusted for a skip when they are very sure
    (SKIP_P), and a missing or odd answer is a failed check."""
    def check():
        s = settings or Settings.load()
        out = (ask or _local_ask)([{"role": "system", "content": LOCAL_SYSTEM},
                                   {"role": "user", "content": f"Question: {question}\n\nFacts:\n{facts[:6000]}"}],
                                  s, timeout)
        if isinstance(out, str):
            out = json.loads(out)
        if isinstance(out, dict) and "why" in out and "data" not in out:
            out = {**out, "data": {"why": out["why"]}}
        return out
    return check


def ollama_ready(settings=None, timeout=1.0):
    """True when Ollama answers and has a model loaded: a gate must never be the thing that makes the GPU load a
    35-billion-parameter model while the owner is at the PC. False on any error."""
    from . import ollama
    try:
        return bool(ollama._post("/api/ps", None, timeout=timeout, settings=settings).get("models"))
    except Exception:  # noqa: BLE001
        return False


# ---- counting ------------------------------------------------------------------------------------------------

def counts(since=0.0, until=None):
    """{"RUN": n, "SKIP": n, "HOLD": n, "BYPASS": n, "skipped_calls": n, "by_gate": {name: skips}} from the events."""
    from . import events
    out = {RUN: 0, SKIP: 0, HOLD: 0, BYPASS: 0, "by_gate": {}}
    for e in events.read("gate", since):
        if until is not None and e.get("time", 0) > until:
            continue
        o = e.get("outcome")
        if o in out:
            out[o] += 1
            if o == SKIP:
                out["by_gate"][e.get("gate", "?")] = out["by_gate"].get(e.get("gate", "?"), 0) + 1
    out["skipped_calls"] = out[SKIP]
    return out


def evening_line(now=None, settings=None):
    """One line for the evening note and the doctor: what the gates and the notify check did today, '' when nothing."""
    import datetime as dt
    now = now or time.time()
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = counts(start)
    parts = []
    if c[SKIP]:
        names = ", ".join(f"{n} {k}" for k, n in sorted(c["by_gate"].items()))
        parts.append(f"gate skipped {c[SKIP]} Claude call{'s' * (c[SKIP] != 1)} ({names})")
    if c[HOLD]:
        parts.append(f"held {c[HOLD]} for morning")
    try:
        from . import events
        moved = sum(1 for e in events.read("notify_check", start) if e.get("notify") is False)
        if moved:
            parts.append(f"{moved} background result{'s' * (moved != 1)} moved into this note instead of a message")
    except Exception:  # noqa: BLE001
        pass
    return ("Gates: " + "; ".join(parts) + ".") if parts else ""


def main(argv=None):
    """python -m waggle.gates [status]: today's counts from the event log (read-only), the switches, and whether the
    local model is loaded (what a local-model gate would use right now)."""
    import datetime as dt
    import sys
    argv = sys.argv[1:] if argv is None else argv
    s = Settings.load()
    start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = counts(start)
    print(f"gates_enabled={enabled(s)} gates_local={getattr(s, 'gates_local', True)} "
          f"notify_check_enabled={getattr(s, 'notify_check_enabled', True)} local model loaded={ollama_ready(s)}")
    print(f"today: RUN {c[RUN]}  SKIP {c[SKIP]}  HOLD {c[HOLD]}  BYPASS {c[BYPASS]}  (skipped Claude calls: "
          f"{c['skipped_calls']})")
    print(evening_line(time.time(), s) or "no gate or notify-check activity today")
    from . import events
    for e in events.read("gate", start)[-8:]:
        print(f"  {time.strftime('%H:%M', time.localtime(e['time']))} {e.get('outcome'):<6} {e.get('gate')}: "
              f"{e.get('reason', '')[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
