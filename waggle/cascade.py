"""Cheap-first cascade for background Claude calls (research idea 6: FrugalGPT, AutoMix, RouteLLM).

    res = cascade.ask("autowork-pick", prompt, schema=SCHEMA, need="answer", system=SYSTEM, check=rule)
    res["json"]  -> the accepted answer      res["tier"] -> "local" | "haiku" | "sonnet"

A background call goes to the local model first (Ollama, answer shaped by a JSON schema), then Haiku, then Sonnet,
and stops at the first answer that passes a cheap self-check:
    - the answer is valid for the schema, AND
    - its `confidence` field (asked for on every tier; added to the schema here and removed from the answer again)
      is at least the threshold for this tier and need, AND
    - the caller's own rule check passes, when it gave one (check(data_or_text) -> bool or (bool, why)).
Sonnet is the last tier: its valid answer is taken whatever its confidence.

Never for the owner's chat: ask() raises CascadeError outside a budget.background(...) context, and the callers
test applies() first. Chat keeps its own path (Claude answers at least 95 % of chat turns; the silent local fallback
was retired for good reason).

Usage steers the cascade (quota.status(): the real 5-hour window and, when known, the seven-day one, the larger of
the two measured against their own stop lines):
    below 50 %          the base thresholds (a stricter self-check, more escalation)
    50 % up to the      the acceptance threshold falls by up to 0.30, so the cheap answer is kept more often:
    background stop     "prefer staying local"
    at or above the     Sonnet is never called; the best answer from Haiku or below is returned, marked
    background stop     low_confidence when it did not clear its threshold
When the background budget refuses a Claude tier (Deferred, a limit, Claude resting) the best valid answer from a
cheaper tier is returned (low_confidence), so work goes on while the window is busy. Only when there is no usable
answer at all does the error of the last tier go to the caller (budget.Deferred or claude_cli.ClaudeError), exactly
what the old direct call raised, so callers keep their handling.

Every step is an event of kind "cascade" (tier, model, accepted, why, score, threshold, usage pressure, ms); the
last step of a call carries final=True and avoided (1 when no Claude call was needed). counts() and evening_line()
add them up. Switch off with config.json "cascade_enabled": false (callers then take their old direct path);
"cascade_local": false skips the local tier only.
"""
import contextlib
import json
import math
import time

from . import budget, claude_cli, events
from .config import Settings

LOCAL, HAIKU, SONNET = "local", "haiku", "sonnet"
ORDER = (LOCAL, HAIKU, SONNET)
NEEDS = ("answer", "judge", "plan")
START = {"answer": LOCAL, "judge": LOCAL, "plan": HAIKU}   # a plan is written by Haiku first: local plans are weak
BASE = {"answer": 0.70, "judge": 0.80, "plan": 0.85}       # acceptance threshold below 50 % usage, on the local tier
FLOOR = {"answer": 0.40, "judge": 0.55, "plan": 0.55}      # never accept below this, however busy the window is
HAIKU_DELTA = -0.10                                         # Haiku is a stronger checker than the local model
PREFER_LOCAL_AT = 0.50
RELIEF = 0.30                                               # the most the threshold falls under usage pressure
LOCAL_TIMEOUT = 90
PRESSURE_TTL_S = 60.0

_NOTE = ("\n\nAlso give a field \"confidence\" (a number from 0 to 1): how sure you are that your answer is right and "
         "complete. Say less than 0.5 when you are guessing or the facts do not settle it.")

_pressure_cache = {"at": 0.0, "value": 0.0}


class CascadeError(claude_cli.ClaudeError):
    """No tier gave a usable answer, or the cascade was used where it must not be. A ClaudeError, so callers that
    already handle a failed Claude call handle this too."""


def enabled(settings=None):
    return bool(getattr(settings or Settings.load(), "cascade_enabled", True))


def applies(settings=None):
    """True when a caller should use the cascade: switched on, and inside background work (never chat)."""
    try:
        return enabled(settings) and budget.is_background()
    except Exception:  # noqa: BLE001
        return False


# ---- usage pressure ---------------------------------------------------------------------------------------------

def current_pressure(s, now=None):
    """How full the owner's usage is, 0..1+: the real five-hour percent, or the seven-day percent scaled so that its
    own stop line (seven_day_stop_at) lands on background_stop_at; whichever is larger. Cached a minute (reading
    the usage scans the transcripts). Any failure reads as 0: the cascade then runs on its base thresholds."""
    now = now or time.time()
    if now - _pressure_cache["at"] < PRESSURE_TTL_S:
        return _pressure_cache["value"]
    value = 0.0
    try:
        from . import quota
        st = quota.status(s)
        value = float(st.get("window_pct") or 0.0) / 100.0
        seven, limit = st.get("seven_day_pct"), quota.seven_day_limit(s)
        if seven is not None and limit:
            value = max(value, float(seven) / 100.0 / limit * float(s.background_stop_at))
    except Exception:  # noqa: BLE001
        value = 0.0
    _pressure_cache.update(at=now, value=value)
    return value


def threshold(tier, need, pressure, stop_line=0.6):
    """The score a tier's answer must reach to be taken without asking the next tier. Sonnet (the last tier) has none."""
    if tier == SONNET:
        return 0.0
    base = BASE[need] + (HAIKU_DELTA if tier == HAIKU else 0.0)
    if pressure > PREFER_LOCAL_AT:
        span = max(float(stop_line) - PREFER_LOCAL_AT, 0.05)
        base -= RELIEF * min(1.0, (pressure - PREFER_LOCAL_AT) / span)
    return round(max(base, FLOOR[need]), 3)


def ceiling(pressure, stop_line=0.6):
    """The highest tier allowed at this usage: Sonnet below the background stop line, Haiku at or above it."""
    return HAIKU if pressure >= float(stop_line) else SONNET


# ---- reading an answer ------------------------------------------------------------------------------------------

_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def valid(data, schema, path="$"):
    """(ok, why) for data against the subset of JSON schema used here: type, enum, required, properties, items,
    minimum/maximum. Unknown keywords are ignored."""
    if not isinstance(schema, dict):
        return True, ""
    typ = schema.get("type")
    if typ in ("number", "integer"):
        if isinstance(data, bool) or not isinstance(data, (int, float)) or not math.isfinite(data):
            return False, f"{path} is not a {typ}"
        if typ == "integer" and float(data) != int(data):
            return False, f"{path} is not an integer"
        if "minimum" in schema and data < schema["minimum"]:
            return False, f"{path} is below {schema['minimum']}"
        if "maximum" in schema and data > schema["maximum"]:
            return False, f"{path} is above {schema['maximum']}"
    elif typ in _TYPES and not isinstance(data, _TYPES[typ]):
        return False, f"{path} is not a {typ}"
    if "enum" in schema and data not in schema["enum"]:
        return False, f"{path} is not one of {schema['enum']}"
    if isinstance(data, dict):
        for key in schema.get("required") or []:
            if key not in data:
                return False, f"{path}.{key} is missing"
        for key, sub in (schema.get("properties") or {}).items():
            if key in data:
                ok, why = valid(data[key], sub, f"{path}.{key}")
                if not ok:
                    return False, why
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(data[:200]):
            ok, why = valid(item, schema["items"], f"{path}[{i}]")
            if not ok:
                return False, why
    return True, ""


def with_confidence(schema):
    """The schema with a required `confidence` number added (a copy), and whether the caller already had one."""
    own = "confidence" in (schema.get("properties") or {})
    out = json.loads(json.dumps(schema))
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    if not own:
        out["properties"]["confidence"] = {"type": "number", "minimum": 0, "maximum": 1,
                                           "description": "how sure you are, 0 to 1"}
        out["required"] = list(out.get("required") or []) + ["confidence"]
    return out, own


def _score(data):
    c = data.get("confidence") if isinstance(data, dict) else None
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c):
        return None
    return min(1.0, max(0.0, float(c)))


def _run_check(check, subject):
    if check is None:
        return True, ""
    try:
        out = check(subject)
    except Exception as e:  # noqa: BLE001 - a rule that crashes is a failed rule
        return False, f"the rule check failed: {type(e).__name__}: {e}"[:160]
    if isinstance(out, tuple):
        return bool(out[0]), str(out[1] if len(out) > 1 else "")[:160]
    return bool(out), "" if out else "the rule check said no"


# ---- the tiers --------------------------------------------------------------------------------------------------

def local_available(s):
    """May the local model be used now? Not switched off, Ollama answers, and either a model is already loaded or the
    owner is away from the PC (a background call must not make the GPU load a 35B model while they work)."""
    if not getattr(s, "cascade_local", True):
        return False, "cascade_local is off"
    from . import gates, ollama
    if gates.ollama_ready(s):
        return True, ""
    try:
        ollama._post("/api/tags", None, timeout=2, settings=s)
    except Exception:  # noqa: BLE001
        return False, "Ollama is not running"
    try:
        from . import presence
        if presence.at_pc():
            return False, "no model loaded and the owner is at the PC"
    except Exception:  # noqa: BLE001
        return False, "could not tell whether the owner is at the PC"
    return True, ""


def _default_local(messages, schema, settings, timeout):
    from . import ollama
    if schema:
        return ollama.chat_json(messages, schema=schema, settings=settings, timeout=timeout)
    return ollama.chat(messages, settings=settings, timeout=timeout)


def _tier_models(s):
    return {HAIKU: s.router_model or "haiku", SONNET: s.deep_model or "sonnet"}


def _plan(s, need, start, pressure, has_check, local_why):
    """([tiers to try, lowest first], [(tier, why it was left out)])."""
    first = start or START[need]
    tiers, left_out = [], []
    models = _tier_models(s)
    for tier in ORDER[ORDER.index(first):]:
        if tier == LOCAL:
            if not has_check:
                left_out.append((tier, "nothing to check a cheap answer against (no schema, no rule)"))
                continue
            if local_why:
                left_out.append((tier, local_why))
                continue
        elif tier == SONNET and ceiling(pressure, s.background_stop_at) == HAIKU:
            left_out.append((tier, f"usage {pressure:.0%} is at the background stop line: never past Haiku"))
            continue
        elif tier == SONNET and models[SONNET] == models[HAIKU]:
            continue  # the same model twice is not an escalation
        tiers.append(tier)
    if not tiers:  # a start above the ceiling still gets its one tier
        tiers = [HAIKU]
    return tiers, left_out


def _call_local(prompt, system, schema, local, s, timeout, purpose):
    messages = [{"role": "system", "content": (system or "") + (_NOTE if schema else "")},
                {"role": "user", "content": prompt}]
    out = (local or _default_local)(messages, schema, s, timeout)
    try:
        budget.record(s.local_model, {}, purpose, local=True)  # a unit-free row, so budget.db shows the call
    except Exception:  # noqa: BLE001
        pass
    if schema:
        if isinstance(out, str):
            from . import ollama
            out = ollama.parse_json(out)
        return out, json.dumps(out, ensure_ascii=False), {}, s.local_model
    return None, str(out), {}, s.local_model


def _call_claude(model, prompt, system, schema, asker, s, effort, timeout, purpose, objective, reserve):
    fn = asker or claude_cli.ask
    have_ticket = bool(budget.current().get("ticket"))
    ctx = (budget.spend(model, purpose=purpose, objective=objective, settings=s) if reserve and not have_ticket
           else contextlib.nullcontext())
    with ctx:
        res = fn(prompt, model=model, system=(system or "") + (_NOTE if schema else ""), schema=schema,
                 effort=effort, purpose=purpose, timeout=timeout) or {}
    data = res.get("json")
    if schema and not isinstance(data, dict):
        try:
            from . import ollama
            data = ollama.parse_json(res.get("text") or "")
        except Exception:  # noqa: BLE001
            data = None
    return data, res.get("text") or "", res.get("usage") or {}, res.get("model_id") or model


def _log(task, need, tier, model, accepted, why, *, score=None, thr=None, pressure=0.0, ms=0, final=False,
         avoided=0, low=False, baseline=None, left_out=None):
    try:
        events.log("cascade", task=str(task)[:60], need=need, tier=tier, model=str(model)[:40], accepted=accepted,
                   why=str(why)[:200], score=score, threshold=thr, pressure=round(pressure, 3), ms=ms, final=final,
                   avoided=avoided, low_confidence=low, baseline=baseline,
                   left_out=[f"{t}: {w}"[:120] for t, w in (left_out or [])] if final else None)
    except Exception:  # noqa: BLE001 - logging never breaks the work
        pass


# ---- the call ---------------------------------------------------------------------------------------------------

class Result(dict):
    """{'text', 'json', 'usage', 'model_id'} like claude_cli.ask, plus tier, accepted, low_confidence, confidence,
    steps (one dict per tier tried)."""


def ask(task, prompt, schema=None, need="answer", *, system=None, check=None, purpose=None, objective=None,
        start=None, baseline="sonnet", effort="low", timeout=300, settings=None, asker=None, local=None,
        pressure=None, reserve=True, local_timeout=LOCAL_TIMEOUT, strict_last=True):
    """One background question, cheapest tier first. See the module doc.
    task        a short name for the log; purpose (default: task) is what the budget ledger records
    need        "answer" | "judge" | "plan": the base acceptance threshold and the first tier (a plan starts at Haiku)
    check       rule check on the answer (the dict with a schema, the text without): bool or (bool, why)
    start       force the first tier ("haiku" to skip the local model)
    baseline    the model the old direct call used, for the avoided-calls count
    asker/local test seams: asker(prompt, model=, system=, schema=, effort=, purpose=, timeout=) -> claude_cli.ask's
                dict; local(messages, schema, settings, timeout) -> dict (with a schema) or text
    pressure    usage 0..1+ (default: current_pressure)
    strict_last False: on the last tier an answer that is valid for the schema but fails the rule check is still
                returned (the caller checks it again itself and has its own way to refuse it)"""
    s = settings or Settings.load()
    if need not in NEEDS:
        raise ValueError(f"need must be one of {NEEDS}")
    if not budget.is_background():
        raise CascadeError("the cascade is for background work only, never the owner's chat", "error")
    purpose = purpose or task
    objective = objective or budget.current().get("objective")
    p = float(pressure if pressure is not None else current_pressure(s))
    stop = float(s.background_stop_at)
    if ORDER.index(start or START[need]) > 0:  # starts above the local model: no need to look for it
        local_why = "the call starts above the local model"
    elif local is not None:  # a test seam: no availability probe
        local_why = "" if getattr(s, "cascade_local", True) else "cascade_local is off"
    else:
        up, why = local_available(s)
        local_why = "" if up else why
    tiers, left_out = _plan(s, need, start, p, schema is not None or check is not None, local_why)
    aug, own_conf = with_confidence(schema) if schema else (None, True)
    models = _tier_models(s)
    steps, best, last_exc, why_all = [], None, None, []

    def strip(data):
        if isinstance(data, dict) and not own_conf:
            data = {k: v for k, v in data.items() if k != "confidence"}
        return data

    for i, tier in enumerate(tiers):
        t0 = time.time()
        model = s.local_model if tier == LOCAL else models[tier]
        is_last = i == len(tiers) - 1
        thr = threshold(tier, need, p, stop)
        try:
            if tier == LOCAL:
                data, text, usage, model_id = _call_local(prompt, system, aug, local, s, local_timeout, purpose)
            else:
                data, text, usage, model_id = _call_claude(model, prompt, system, aug, asker, s, effort, timeout,
                                                           purpose, objective, reserve)
        except budget.Deferred as e:
            last_exc = e
            steps.append({"tier": tier, "accepted": False, "why": f"deferred: {e.reason}"})
            why_all.append(f"{tier}: deferred")
            _log(task, need, tier, model, False, f"the background budget said wait: {e.reason}", pressure=p,
                 ms=int((time.time() - t0) * 1000))
            break  # a dearer tier would be refused too
        except Exception as e:  # noqa: BLE001 - any failed call: the next tier, or the caller's old handling
            last_exc = e
            steps.append({"tier": tier, "accepted": False, "why": f"{type(e).__name__}: {e}"[:160]})
            why_all.append(f"{tier}: {type(e).__name__}")
            _log(task, need, tier, model, False, f"call failed: {type(e).__name__}: {e}", pressure=p,
                 ms=int((time.time() - t0) * 1000))
            if getattr(e, "kind", None) in ("limit", "auth", "missing") and tier != LOCAL:
                break  # Claude itself is unavailable: dearer tiers will not do better
            continue
        subject = data if schema else text
        if schema:  # Sonnet is not asked to rate itself, so it need not carry the added confidence field
            ok, why = valid(data, schema if tier == SONNET else aug)
        else:
            ok, why = bool((text or "").strip()), "an empty answer"
        score = _score(data) if schema else None
        if ok:
            rule_ok, rule_why = _run_check(check, strip(subject) if schema else subject)
            if not rule_ok and (strict_last or not is_last):
                ok, why = False, rule_why
            elif not rule_ok:
                why = rule_why  # kept: the caller's own check refuses it
        if ok and schema and score is None and tier != SONNET:
            ok, why = False, "no confidence given"
        steps.append({"tier": tier, "accepted": False, "why": why, "score": score})
        if not ok:
            why_all.append(f"{tier}: {why}")
            _log(task, need, tier, model, False, f"unusable: {why}", score=score, thr=thr, pressure=p,
                 ms=int((time.time() - t0) * 1000))
            continue
        cleared = tier == SONNET or schema is None or (score is not None and score >= thr)
        cand = {"tier": tier, "data": strip(data), "text": text, "usage": usage, "model_id": model_id,
                "score": score, "threshold": thr, "cleared": cleared}
        if cleared or is_last:
            low = not cleared
            steps[-1].update(accepted=True, why="clears the threshold" if cleared else "the ceiling: best available")
            avoided = 1 if tier == LOCAL else 0
            _log(task, need, tier, model, True, steps[-1]["why"], score=score, thr=thr, pressure=p,
                 ms=int((time.time() - t0) * 1000), final=True, avoided=avoided, low=low, baseline=baseline,
                 left_out=left_out)
            return _result(cand, steps, low)
        steps[-1]["why"] = f"confidence {score:.2f} is below {thr:.2f}: asking the next tier"
        _log(task, need, tier, model, False, steps[-1]["why"], score=score, thr=thr, pressure=p,
             ms=int((time.time() - t0) * 1000))
        if best is None or (score or 0) > (best["score"] or 0):
            best = cand
    if best is not None:  # a cheaper tier had a valid answer and the dearer ones could not be reached
        steps.append({"tier": best["tier"], "accepted": True, "why": "dearer tiers unavailable: best available"})
        _log(task, need, best["tier"], best["model_id"], True, "dearer tiers unavailable: best available",
             score=best["score"], thr=best["threshold"], pressure=p, final=True,
             avoided=1 if best["tier"] == LOCAL else 0, low=True, baseline=baseline, left_out=left_out)
        return _result(best, steps, True)
    _log(task, need, "none", "", False, "; ".join(why_all) or "no tier tried", pressure=p, final=True,
         baseline=baseline, left_out=left_out)
    if last_exc is not None:
        raise last_exc
    raise CascadeError(f"no tier gave a usable answer: {'; '.join(why_all)}"[:300], "error")


def _result(cand, steps, low):
    r = Result(text=cand["text"], json=cand["data"], usage=cand["usage"], model_id=cand["model_id"])
    r.update(tier=cand["tier"], accepted=True, low_confidence=low, confidence=cand["score"], steps=steps)
    return r


# ---- counting ---------------------------------------------------------------------------------------------------

def counts(since=0.0, until=None):
    """{"calls", "by_tier": {local, haiku, sonnet}, "avoided", "downgraded", "rejected", "low_confidence", "failed"}
    from the events: one call = one event with final=True."""
    out = {"calls": 0, "by_tier": {LOCAL: 0, HAIKU: 0, SONNET: 0}, "avoided": 0, "downgraded": 0, "rejected": 0,
           "low_confidence": 0, "failed": 0}
    for e in events.read("cascade", since):
        if until is not None and e.get("time", 0) > until:
            continue
        if not e.get("final"):
            if e.get("accepted") is False and e.get("tier") in ORDER:
                out["rejected"] += 1
            continue
        if e.get("tier") not in ORDER:
            out["failed"] += 1
            continue
        out["calls"] += 1
        out["by_tier"][e["tier"]] += 1
        out["avoided"] += int(e.get("avoided") or 0)
        if e["tier"] == HAIKU and (e.get("baseline") or "") == SONNET:
            out["downgraded"] += 1
        out["low_confidence"] += 1 if e.get("low_confidence") else 0
    return out


def evening_line(now=None, settings=None):
    """One line for the evening note and the doctor, '' when the cascade did nothing today."""
    import datetime as dt
    now = now or time.time()
    start = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = counts(start)
    if not c["calls"] and not c["failed"]:
        return ""
    bt = c["by_tier"]
    parts = [f"{c['calls']} background answer{'s' * (c['calls'] != 1)}: {bt[LOCAL]} stayed local "
             f"({c['avoided']} Claude call{'s' * (c['avoided'] != 1)} avoided), {bt[HAIKU]} on Haiku, "
             f"{bt[SONNET]} on Sonnet"]
    if c["downgraded"]:
        parts.append(f"{c['downgraded']} Haiku answer{'s' * (c['downgraded'] != 1)} where Sonnet used to run")
    if c["low_confidence"]:
        parts.append(f"{c['low_confidence']} taken below the usual confidence (usage was high or Claude unavailable)")
    if c["failed"]:
        parts.append(f"{c['failed']} with no usable answer")
    return "Cascade: " + "; ".join(parts) + "."


def main(argv=None):
    """python -m waggle.cascade status | try "question" [--need judge] [--start haiku]
    status: today's counts from the event log (read-only).
    try:    one real background question through the cascade, to see which tier answers (writes events and a
            ledger row into the data home: set WAGGLE_HOME to a scratch folder to keep your own clean)."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    s = Settings.load()
    if argv[:1] == ["try"] and len(argv) >= 2:
        need = argv[argv.index("--need") + 1] if "--need" in argv else "answer"
        start = argv[argv.index("--start") + 1] if "--start" in argv else None
        schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
        with budget.background("cascade-try", user_started=True):
            try:
                r = ask("try", argv[1], schema, need, system="Answer briefly. JSON only: {\"answer\": \"...\"}",
                        start=start, settings=s)
            except Exception as e:  # noqa: BLE001
                print(f"no answer: {type(e).__name__}: {e}")
                return 1
        print(json.dumps({"tier": r["tier"], "model": r["model_id"], "answer": r["json"],
                          "confidence": r["confidence"], "low_confidence": r["low_confidence"],
                          "steps": r["steps"]}, indent=2, default=str))
        return 0
    import datetime as dt
    start_t = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = counts(start_t)
    print(f"cascade_enabled={enabled(s)} cascade_local={getattr(s, 'cascade_local', True)} "
          f"usage pressure={current_pressure(s):.0%} (background stop {s.background_stop_at:.0%})")
    print(evening_line(time.time(), s) or "no cascade activity today")
    print(f"today: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
